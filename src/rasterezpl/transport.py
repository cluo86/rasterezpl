"""Getting the bytes to the printer: TCP 9100, USB printer class (pyusb), a raw CUPS queue, a Windows print
queue, or a file.

The printer only ever sees a byte stream; it does not know what produced it. That is why no driver is needed.

* ``tcp://host[:9100]`` — Godex LAN ports and the Panduit NetPS module speak raw on 9100. Plain sockets: the same
  on every OS, and the common floor case (a TDP43ME with the NetPS board, a G500 with its Ethernet option).
* ``usb:``, ``usb:<serial>``, ``usb:<name part>``, ``usb:VID:PID`` — the USB printer-class device itself (pyusb +
  libusb). macOS 26 dropped raw CUPS queues ("Raw queues are no longer supported"), so on a Mac this is the way.
  Godex and Panduit printers can share one VID:PID (195f:0001), so select by serial or product name. On Windows
  the printer-class driver owns the device and libusb cannot claim it (a WinUSB driver swapped in with Zadig
  would, which a site laptop will not get): use ``win:`` there.
* ``lp:<queue>`` — a raw CUPS queue (Linux: ``lpadmin -p x -E -v usb://… -m raw``).
* ``win:<queue name>`` — a Windows print queue, written through the spooler with the RAW datatype (pywin32,
  extra ``rasterezpl[windows]``): the bytes pass through unchanged — no driver rendering, the job IS the raster —
  and the queue's own port (USB, TCP/IP) is the pipe, so the driver the laptop already has is enough.
  ``rasterezpl queues`` lists the queues (and on macOS / Linux the CUPS queues for ``lp:``).
* anything else — a file path; the bytes are written for later.

``~S,CHECK`` reads the printer state back (00 = ready) over TCP or over the USB IN endpoint. A queue (``lp:``,
``win:``) has no such channel: the status says so, and ``win:`` adds what the spooler knows (its status word
and the jobs waiting).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .media import EOL, Media, offset_commands
from .stream import count_labels

USB_PRINTER_CLASS = 7
USB_CHUNK = 16384

WIN_USB_NOTE = (
    "on Windows the printer-class driver (usbprint.sys) owns a USB printer and libusb cannot claim it — print "
    "through the printer's Windows queue instead: win:<queue name> (`rasterezpl queues` lists them), or swap a "
    "WinUSB driver in with Zadig for usb:"
)


def _usb_modules():
    """pyusb's core and util, or a RuntimeError saying what to install (and, on Windows, what to use instead)."""
    try:
        import usb.core
        import usb.util
    except ImportError:
        raise RuntimeError(
            'usb: needs pyusb (pip install "rasterezpl[usb]") and libusb'
            + (
                f" — {WIN_USB_NOTE}"
                if sys.platform == "win32"
                else (" (brew install libusb)" if sys.platform == "darwin" else " (apt install libusb-1.0-0)")
            )
        ) from None
    return usb.core, usb.util


def _usb_find_all(core):
    """Every USB device, or a RuntimeError when libusb has no backend (not installed; on Windows the usual case)."""
    try:
        return list(core.find(find_all=True))
    except core.NoBackendError:
        raise RuntimeError(
            "pyusb found no libusb backend"
            + (
                f" — {WIN_USB_NOTE}"
                if sys.platform == "win32"
                else " (brew install libusb / apt install libusb-1.0-0)"
            )
        ) from None


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
    core, _util = _usb_modules()
    import usb.util

    out = []
    for d in _usb_find_all(core):
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
        core, util = _usb_modules()
        import usb.util

        self._util = usb.util
        devs = [
            d
            for d in _usb_find_all(core)
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
        try:
            self._util.claim_interface(self.dev, self.intf.bInterfaceNumber)
        except Exception as e:  # usb.core.USBError: access denied / not supported — another driver holds it
            if sys.platform == "win32":
                raise RuntimeError(
                    f"{self.name}: cannot claim the USB interface ({e}) — {WIN_USB_NOTE}"
                ) from None
            raise RuntimeError(
                f"{self.name}: cannot claim the USB interface ({e}) — another process (CUPS, a vendor tool) holds it?"
            ) from None
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


def _win32print():
    """pywin32's ``win32print``, or a RuntimeError saying what to install. Imported on demand: the module exists
    on Windows only, and the rest of the package must import everywhere."""
    try:
        import win32print
    except ImportError:
        raise RuntimeError(
            'win:<queue> needs pywin32 (pip install "rasterezpl[windows]") — the Windows spooler\'s RAW datatype'
            + ("" if sys.platform == "win32" else "; a Windows print queue is reachable from Windows only")
        ) from None
    return win32print


def list_win_queues() -> list[dict[str, Any]]:
    """The Windows print queues this user can print to (local and connected shares): name, port, driver, the
    spooler's status word (0 = no error flag), the jobs waiting, and whether it is the default. Each is a
    ``win:<name>`` target."""
    wp = _win32print()
    try:
        default = wp.GetDefaultPrinter()
    except Exception:  # no default printer set
        default = None
    out = []
    for d in wp.EnumPrinters(wp.PRINTER_ENUM_LOCAL | wp.PRINTER_ENUM_CONNECTIONS, None, 2):
        name = str(d.get("pPrinterName", ""))
        out.append(
            {
                "name": name,
                "port": str(d.get("pPortName", "") or ""),
                "driver": str(d.get("pDriverName", "") or ""),
                "status": int(d.get("Status", 0) or 0),
                "jobs": int(d.get("cJobs", 0) or 0),
                "default": name == default,
            }
        )
    return out


def list_lp_queues() -> list[dict[str, Any]]:
    """The CUPS queues ``lpstat -p`` knows (macOS / Linux): name, its state line, and whether it is the default
    (``lpstat -d``). Each is an ``lp:<name>`` target — a RAW queue, or the bytes are rendered by a driver."""
    try:
        r = subprocess.run(["lpstat", "-p"], capture_output=True, text=True)
        d = subprocess.run(["lpstat", "-d"], capture_output=True, text=True)
    except FileNotFoundError:
        return []
    default = d.stdout.rpartition(":")[2].strip() if d.returncode == 0 and ":" in d.stdout else None
    out = []
    for line in r.stdout.splitlines():
        if not line.startswith("printer "):
            continue
        name = line.split()[1]
        out.append({"name": name, "state": line.partition(name)[2].strip(), "default": name == default})
    return out


def list_queues() -> list[dict[str, Any]]:
    """The print queues of this OS: :func:`list_win_queues` on Windows, :func:`list_lp_queues` elsewhere."""
    return list_win_queues() if sys.platform == "win32" else list_lp_queues()


def status_win(queue: str) -> str:
    """What the spooler knows about a Windows queue — there is no ``~S,CHECK`` over a queue, so the answer never
    starts with ``00``: a ``--status`` print refuses, as it does for ``lp:``. Says the spooler's status word (0 =
    no error flag set: paused, offline, out of paper … are bits of it) and the jobs waiting."""
    wp = _win32print()
    try:
        h = wp.OpenPrinter(queue)
    except Exception as e:
        return f"win queue {queue!r}: cannot open it ({e}); `rasterezpl queues` lists the queues"
    try:
        info = wp.GetPrinter(h, 2)
    finally:
        wp.ClosePrinter(h)
    st, jobs = int(info.get("Status", 0) or 0), int(info.get("cJobs", 0) or 0)
    return (
        f"win queue {queue!r}: no ~S,CHECK over a queue; spooler status {st}"
        + ("" if st == 0 else " (an error flag is set: paused, offline, paper or ribbon?)")
        + f", {jobs} job{'s' if jobs != 1 else ''} waiting"
    )


def send_win(data: bytes, queue: str, doc_name: str = "rasterezpl job") -> int:
    """Stream a job to a Windows print queue through the spooler with the RAW datatype: the spooler passes the
    bytes through unchanged to the queue's port (USB, TCP/IP, LPT), so the driver the laptop already has is the
    pipe and no WinUSB / libusb swap is needed. Chunked like :func:`send_tcp`; ``WritePrinter`` may take fewer
    bytes than given and the rest is written again.

    Accepted by the spooler is not printed: the spooler holds the job in its spool file and feeds the port as the
    printer takes bytes — the END label of ``--bookends`` on the roll is the gate, here as on an Ethernet board.
    A failure after some bytes aborts the spooled document (nothing more is fed) and raises :class:`SendError`
    with the bytes the spooler had taken — what of them reached the printer depends on the queue's "start
    printing immediately" setting: read the roll."""
    wp = _win32print()
    try:
        h = wp.OpenPrinter(queue)
    except Exception as e:  # pywintypes.error: no such queue, access denied
        raise SendError(
            f"win:{queue}: cannot open the queue ({e}); `rasterezpl queues` lists them; nothing sent", 0
        ) from None
    sent = 0
    try:
        try:
            wp.StartDocPrinter(
                h, 1, (doc_name, None, "RAW")
            )  # level 1: (document name, output file, datatype)
        except Exception as e:
            raise SendError(f"win:{queue}: the spooler refused the document ({e}); nothing sent", 0) from None
        try:
            wp.StartPagePrinter(h)
            view = memoryview(data)
            while sent < len(view):
                n = int(wp.WritePrinter(h, bytes(view[sent : sent + TCP_CHUNK])))
                if n <= 0:
                    raise OSError("WritePrinter wrote 0 bytes")
                sent += n
            wp.EndPagePrinter(h)
            wp.EndDocPrinter(h)
        except Exception as e:
            try:
                wp.AbortPrinter(h)  # drop the partial document: the spooler feeds the printer nothing more
            except Exception:
                pass
            raise SendError(
                f"win:{queue}: the spooler stopped taking data after {sent} bytes of {len(data)} ({e}); the "
                "partial document was aborted — what reached the printer depends on the queue's spooling "
                "setting: read the roll",
                sent,
            ) from None
    finally:
        wp.ClosePrinter(h)
    return sent


def status(target: str) -> str:
    """``~S,CHECK`` for a tcp:// or usb: target ('' when the printer is silent). A file spool is always ready:
    ``00 file spool`` when its directory can take the file, else a message. An lp: queue has no status here;
    a win: queue reports what the spooler knows (never ``00``)."""
    if target.startswith("tcp://"):
        host, _, port = target[6:].partition(":")
        return status_tcp(host, int(port) if port else 9100)
    if target.startswith("usb:"):
        return status_usb(target[4:])
    if target.startswith("lp:"):
        return f"lp queue {target[3:]!r}: no status over lp (lpstat knows)"
    if target.startswith("win:"):
        return status_win(target[4:])
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
    if m is None:
        if offset_in != (0.0, 0.0):
            raise ValueError("an offset needs the media (its dpi)")
    else:  # always — a zero too, or the printer prints with the registration the LAST job left in it
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
    if target.startswith("win:"):
        q = target[4:]
        n = send_win(data, q)
        return f"spooled {n} bytes ({n_labels} labels) on Windows queue {q!r} as RAW", n
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
        shown = f"{settle:.1f}".rstrip("0").rstrip(".")
        return f"settled {shown} s before the next job (no status query)"
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
