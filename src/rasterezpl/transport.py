"""Getting the bytes to the printer: TCP 9100, USB printer class (pyusb), a raw CUPS queue, or a file.

The printer only ever sees a byte stream; it does not know what produced it. That is why no driver is needed.

* ``tcp://host[:9100]`` — Godex LAN ports and the Panduit NetPS module speak raw on 9100.
* ``usb:``, ``usb:<serial>``, ``usb:<name part>``, ``usb:VID:PID`` — the USB printer-class device itself (pyusb +
  libusb). macOS 26 dropped raw CUPS queues ("Raw queues are no longer supported"), so on a Mac this is the way.
  Godex and Panduit printers can share one VID:PID (195f:0001), so select by serial or product name.
* ``lp:<queue>`` — a raw CUPS queue (Linux: ``lpadmin -p x -E -v usb://… -m raw``).
* anything else — a file path; the bytes are written for later.

``~S,CHECK`` reads the printer state back (00 = ready) over TCP or over the USB IN endpoint.
"""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path
from typing import Any

from .media import EOL, Media, offset_commands
from .stream import count_labels

USB_PRINTER_CLASS = 7
USB_CHUNK = 16384


def status_tcp(host: str, port: int = 9100, timeout: float = 3.0) -> str:
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.sendall(b"~S,CHECK" + EOL)
        s.settimeout(timeout)
        try:
            return s.recv(64).decode("ascii", "replace").strip()
        except TimeoutError:
            return ""


def _usb_matches(d, spec: str, util) -> bool:
    if not spec:
        return True
    s = spec.strip().lower()
    if ":" in s:
        v, _, p = s.partition(":")
        try:
            return (d.idVendor, d.idProduct) == (int(v, 16), int(p, 16))
        except ValueError:
            pass
    sn = (util.get_string(d, d.iSerialNumber) if d.iSerialNumber else "") or ""
    name = f"{util.get_string(d, d.iManufacturer) or ''} {util.get_string(d, d.iProduct) or ''}"
    return s == sn.lower() or s in name.lower()


def list_usb_printers() -> list[dict[str, str]]:
    """Every USB printer-class device on the bus: vid:pid, manufacturer, product, serial."""
    import usb.core
    import usb.util

    out = []
    for d in usb.core.find(find_all=True):
        if not any(i.bInterfaceClass == USB_PRINTER_CLASS for cfg in d for i in cfg):
            continue
        out.append(
            {
                "ids": f"{d.idVendor:04x}:{d.idProduct:04x}",
                "manufacturer": usb.util.get_string(d, d.iManufacturer) or "",
                "product": usb.util.get_string(d, d.iProduct) or "",
                "serial": (usb.util.get_string(d, d.iSerialNumber) if d.iSerialNumber else "") or "",
            }
        )
    return out


class UsbPrinter:
    """Claims the USB printer interface for the duration of a ``with`` block; ``write`` streams a job, ``read``
    collects a reply if the device has an IN endpoint."""

    def __init__(self, spec: str = ""):
        import usb.core
        import usb.util

        self._util = usb.util
        devs = [
            d
            for d in usb.core.find(find_all=True)
            if any(i.bInterfaceClass == USB_PRINTER_CLASS for cfg in d for i in cfg)
            and _usb_matches(d, spec, usb.util)
        ]
        if len(devs) != 1:
            names = [
                f"{usb.util.get_string(d, d.iProduct) or ''} serial {usb.util.get_string(d, d.iSerialNumber) or '?'}"
                for d in devs
            ]
            raise RuntimeError(
                f"{len(devs)} USB printer-class devices match {spec!r}: {names}; name one as usb:<serial> or usb:<name>"
                if devs
                else "no USB printer-class device on the bus (plugged in and switched on? pyusb + libusb installed?)"
            )
        self.dev = devs[0]
        self.serial = (
            usb.util.get_string(self.dev, self.dev.iSerialNumber) if self.dev.iSerialNumber else ""
        ) or ""
        self.name = (
            f"{usb.util.get_string(self.dev, self.dev.iManufacturer) or ''} "
            f"{usb.util.get_string(self.dev, self.dev.iProduct) or ''}"
        ).strip()
        self.intf: Any = None
        self.ep_out: Any = None
        self.ep_in: Any = None

    def __enter__(self) -> UsbPrinter:
        try:
            cfg = self.dev.get_active_configuration()
        except Exception:
            self.dev.set_configuration()
            cfg = self.dev.get_active_configuration()
        self.intf = next(i for i in cfg if i.bInterfaceClass == USB_PRINTER_CLASS)
        try:
            if self.dev.is_kernel_driver_active(self.intf.bInterfaceNumber):
                self.dev.detach_kernel_driver(self.intf.bInterfaceNumber)
        except Exception:
            pass  # macOS: nothing to detach; CUPS only holds the device during a job
        self._util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        u = self._util
        self.ep_out = u.find_descriptor(
            self.intf, custom_match=lambda e: u.endpoint_direction(e.bEndpointAddress) == u.ENDPOINT_OUT
        )
        self.ep_in = u.find_descriptor(
            self.intf, custom_match=lambda e: u.endpoint_direction(e.bEndpointAddress) == u.ENDPOINT_IN
        )
        if self.ep_out is None:
            raise RuntimeError(f"{self.name}: printer interface has no bulk OUT endpoint")
        return self

    def __exit__(self, *_exc) -> None:
        if self.intf is not None:
            self._util.release_interface(self.dev, self.intf.bInterfaceNumber)
        self._util.dispose_resources(self.dev)

    def write(self, data: bytes, timeout_ms: int = 10000, deadline_s: float = 600.0) -> int:
        """Stream a job. A big job fills the printer's input buffer and the printer then takes data only as fast
        as it prints, so a chunk write can time out while the printer is busy: that chunk is retried until
        ``deadline_s`` (2026-09-26: eight 17 KB rows back to back timed out at the default 10 s)."""
        import time

        import usb.core

        sent = 0
        t_end = time.monotonic() + deadline_s
        for i in range(0, len(data), USB_CHUNK):
            chunk = data[i : i + USB_CHUNK]
            while True:
                try:
                    sent += self.ep_out.write(chunk, timeout=timeout_ms)
                    break
                except usb.core.USBTimeoutError:
                    if time.monotonic() > t_end:
                        raise RuntimeError(
                            f"{self.name}: printer stopped taking data for {deadline_s:.0f} s after {sent} bytes "
                            "(paused, out of media or ribbon, or a jam?)"
                        ) from None
        return sent

    def read(self, timeout_ms: int = 2000) -> str:
        if self.ep_in is None:
            return ""
        try:
            return bytes(self.ep_in.read(64, timeout=timeout_ms)).decode("ascii", "replace").strip()
        except Exception:  # timeout: the printer said nothing
            return ""


def status_usb(spec: str = "") -> str:
    with UsbPrinter(spec) as pr:
        pr.write(b"~S,CHECK" + EOL)
        return pr.read()


def status(target: str) -> str:
    """``~S,CHECK`` for a tcp:// or usb: target ('' when the printer is silent). A file spool is always ready:
    ``00 file spool`` when its directory can take the file, else a message. An lp: queue has no status here."""
    if target.startswith("tcp://"):
        host, _, port = target[6:].partition(":")
        return status_tcp(host, int(port) if port else 9100)
    if target.startswith("usb:"):
        return status_usb(target[4:])
    if target.startswith("lp:"):
        return f"lp queue {target[3:]!r}: no status over lp (lpstat knows)"
    from pathlib import Path

    d = Path(target).expanduser().parent
    if d.is_dir() and os.access(d, os.W_OK):
        return "00 file spool (writable)"
    return f"file spool: {d} is not a writable directory"


def send(
    data: bytes, target: str, m: Media | None = None, offset_in: tuple[float, float] = (0.0, 0.0)
) -> str:
    """Deliver a job to a target (see the module docstring); ``offset_in`` = that printer's registration, prefixed
    as ^R / ~Q. Returns a one-line note."""
    if offset_in != (0.0, 0.0):
        if m is None:
            raise ValueError("an offset needs the media (its dpi)")
        data = offset_commands(m, offset_in) + data
    n_labels = count_labels(data)
    if target.startswith("tcp://"):
        host, _, p = target[6:].partition(":")
        port = int(p) if p else 9100
        with socket.create_connection((host, port), timeout=10) as s:
            s.sendall(data)
        return f"sent {len(data)} bytes ({n_labels} labels) to {host}:{port}"
    if target.startswith("usb:"):
        with UsbPrinter(target[4:]) as pr:
            n = pr.write(data)
        return f"sent {n} bytes ({n_labels} labels) to {pr.name} over USB"
    if target.startswith("lp:"):
        q = target[3:]
        r = subprocess.run(["lp", "-d", q, "-o", "raw", "-"], input=data, capture_output=True)
        if r.returncode:
            raise RuntimeError(f"lp failed: {r.stderr.decode(errors='replace').strip()}")
        return f"queued {len(data)} bytes ({n_labels} labels) on {q}: {r.stdout.decode().strip()}"
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return f"wrote {out} ({n_labels} labels)"
