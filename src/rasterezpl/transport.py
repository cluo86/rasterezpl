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
import time
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


TCP_CHUNK = 64 * 1024
READY_BACKOFF_S = (
    0.5,
    1.0,
    2.0,
    4.0,
    8.0,
    15.0,
)  # between attempts while the port refuses, times out or resets
PROBE_S = (
    0.3  # after connecting: how long to watch for the printer closing the fresh connection before any byte
)


class SendError(RuntimeError):
    """A send that did not complete. ``bytes_sent`` = what the printer had accepted when it stopped — zero means
    nothing is on the paper and the job may simply be sent again; more means labels ARE on the paper and a resend
    would print them twice: read the roll and resume with ``--labels``."""

    def __init__(self, message: str, bytes_sent: int = 0):
        super().__init__(message)
        self.bytes_sent = bytes_sent


def _connect_when_ready(host: str, port: int, t_ready: float, timeout_s: float) -> socket.socket:
    """A connection to the print port, waiting with backoff while it refuses or times out — a board that is
    printing from its buffer, or restarting its print server, is NOT READY, not a failed job. Raises
    :class:`SendError` (0 bytes) at the deadline."""
    k = 0
    while True:
        try:
            return socket.create_connection((host, port), timeout=timeout_s)
        except OSError as e:
            if time.monotonic() > t_ready:
                raise SendError(
                    f"{host}:{port}: not ready — {e.__class__.__name__}: {e} (the board is printing or "
                    "restarting its print server?); nothing sent",
                    0,
                ) from None
            time.sleep(
                min(READY_BACKOFF_S[min(k, len(READY_BACKOFF_S) - 1)], max(0.0, t_ready - time.monotonic()))
            )
            k += 1


def _peer_alive(s: socket.socket, probe_s: float) -> bool:
    """Whether the printer keeps a fresh connection open: a board that resets a connection opened while it is
    still printing closes it at once — seen here BEFORE any byte goes, so the job can be retried with nothing on
    the paper. Silence for ``probe_s`` = alive."""
    s.settimeout(probe_s)
    try:
        return s.recv(1, socket.MSG_PEEK) != b""
    except TimeoutError:
        return True
    except OSError:
        return False


def send_tcp(
    data: bytes,
    host: str,
    port: int = 9100,
    timeout_s: float = 30.0,
    deadline_s: float = 900.0,
    ready_s: float = 900.0,
    probe_s: float = PROBE_S,
) -> int:
    """Stream a job over raw TCP (port 9100) the way the USB path does: in chunks, retrying a stalled chunk until
    ``deadline_s``. The printer's Ethernet board takes a big job only as fast as the printer prints it, so one
    ``sendall`` of a 128-label job (~1 MB) stalls past the socket timeout and dies with TimeoutError after part of
    the job is in the printer (2026-10-16: the first bundle of an eight-bundle cart on the TDP43ME NetPS board).

    Before the first byte the port is waited on (refused / timed out / closed at once = not ready, backoff up to
    ``ready_s``): the NetPS board resets a connection opened while it still prints the previous job from its
    buffer, and the reset can wedge port 9100 until the board restarts its print server (~90 s, "Restart Server
    is ongoing" on its status page) — seen 2026-10-16 when the second file of a cart followed the first at once.
    A connection the printer closes after bytes were accepted is NEVER retried: those labels are on the paper.

    Returns the bytes sent; raises :class:`SendError` naming host:port and the bytes sent so far when the
    printer stops taking data or closes the connection."""
    t_ready = time.monotonic() + ready_s
    k = 0
    while True:
        s = _connect_when_ready(host, port, t_ready, timeout_s)
        sent = 0
        with s:
            if not _peer_alive(s, probe_s):
                if time.monotonic() > t_ready:
                    raise SendError(f"{host}:{port}: closes every connection at once; nothing sent", 0)
                time.sleep(READY_BACKOFF_S[min(k, len(READY_BACKOFF_S) - 1)])
                k += 1
                continue
            s.settimeout(timeout_s)
            t_end = time.monotonic() + deadline_s
            view = memoryview(data)
            try:
                while sent < len(view):
                    try:
                        n = s.send(view[sent : sent + TCP_CHUNK])
                        if n == 0:
                            raise ConnectionResetError("send returned 0")
                        sent += n
                    except TimeoutError:
                        if time.monotonic() > t_end:
                            raise SendError(
                                f"{host}:{port}: printer stopped taking data for {deadline_s:.0f} s after {sent} "
                                "bytes (paused, out of media or ribbon, or a jam?)",
                                sent,
                            ) from None
            except OSError as e:  # BrokenPipeError, ConnectionResetError, any socket error
                if sent == 0 and time.monotonic() < t_ready:
                    time.sleep(READY_BACKOFF_S[min(k, len(READY_BACKOFF_S) - 1)])
                    k += 1
                    continue  # nothing on the paper: the job goes again
                raise SendError(
                    f"{host}:{port}: connection closed by the printer after {sent} bytes "
                    f"({e.__class__.__name__}: {e})",
                    sent,
                ) from None
        return sent


def deliver(
    data: bytes, target: str, m: Media | None = None, offset_in: tuple[float, float] = (0.0, 0.0)
) -> tuple[str, int]:
    """Deliver a job to a target (see the module docstring); ``offset_in`` = that printer's registration, prefixed
    as ^R / ~Q. Returns (a one-line note, the bytes delivered). A send the printer cuts short raises
    :class:`SendError` with the bytes it had accepted."""
    if offset_in != (0.0, 0.0):
        if m is None:
            raise ValueError("an offset needs the media (its dpi)")
        data = offset_commands(m, offset_in) + data
    n_labels = count_labels(data)
    if target.startswith("tcp://"):
        host, _, p = target[6:].partition(":")
        port = int(p) if p else 9100
        sent = send_tcp(data, host, port)
        return f"sent {sent} bytes ({n_labels} labels) to {host}:{port}", sent
    if target.startswith("usb:"):
        with UsbPrinter(target[4:]) as pr:
            n = pr.write(data)
        return f"sent {n} bytes ({n_labels} labels) to {pr.name} over USB", n
    if target.startswith("lp:"):
        q = target[3:]
        r = subprocess.run(["lp", "-d", q, "-o", "raw", "-"], input=data, capture_output=True)
        if r.returncode:
            raise RuntimeError(f"lp failed: {r.stderr.decode(errors='replace').strip()}")
        return f"queued {len(data)} bytes ({n_labels} labels) on {q}: {r.stdout.decode().strip()}", len(data)
    out = Path(target)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return f"wrote {out} ({n_labels} labels)", len(data)


def send(
    data: bytes, target: str, m: Media | None = None, offset_in: tuple[float, float] = (0.0, 0.0)
) -> str:
    """:func:`deliver`, returning the note alone (the library's original signature)."""
    return deliver(data, target, m, offset_in)[0]


def wait_idle(target: str, settle: float = 0.0, deadline_s: float = 900.0) -> str:
    """Between two jobs on ONE printer — "send complete" is not "printed": an Ethernet board accepts a 1.2 MB
    job in a minute and prints it from its buffer over the next several, and a connection opened meanwhile is
    reset (2026-10-16, NetPS). ``settle`` > 0 = wait that long and ask the printer nothing (the way that opens no
    connection while it prints — README recommends 180 s for the NetPS board); else poll ``~S,CHECK`` with
    backoff until it answers ``00`` (idle) or ``deadline_s`` passes (a RuntimeError — the next job is not sent).
    A printer whose status is unavailable or silent gets no wait at all — the note says so. Returns the note."""
    if settle > 0:
        time.sleep(settle)
        return f"settled {settle:g} s before the next job (no status query)"
    t_end = time.monotonic() + deadline_s
    k = 0
    while True:
        try:
            st = status(target)
        except Exception as e:  # a port that refuses while the board prints: no answer is an answer
            st = ""
            why = f"{e.__class__.__name__}: {e}"
        else:
            why = "no reply"
        if st.startswith("00"):
            return f"printer ready ({st})" if k == 0 else f"printer ready after {k} status polls ({st})"
        if not st:
            return f"status unavailable ({why}) — no wait before the next job (--settle N waits N s instead)"
        if time.monotonic() > t_end:
            raise RuntimeError(
                f"{target}: still busy after {deadline_s:.0f} s (status {st!r}); next job not sent"
            )
        time.sleep(READY_BACKOFF_S[min(k, len(READY_BACKOFF_S) - 1)])
        k += 1
