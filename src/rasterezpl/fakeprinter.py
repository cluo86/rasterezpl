"""A fake EZPL printer on a TCP port — the Panduit TDP43ME's NetPS Ethernet board as it was learned by losing
labels (2026-10-16/17), as a pytest fixture, a library object and ``rasterezpl fakeprinter``. Never a real printer:
it records every byte it accepted, per connection, and decodes a job to pages with the package's own decoder.

What the board does, and the knob that models it (``Profile``):

* it ACCEPTS a job into a buffer and prints from it for minutes afterwards ("sent" is not "printed") —
  ``buffer_bytes`` and ``drain_bps``: the port stops reading when the buffer is full and takes bytes again as the
  drain frees room, so the sender's TCP window fills and a chunked send rides the stall, as on the real board;
* a NEW connection opened while it still prints is RESET at once — ``reset_while_busy`` (busy = bytes still in
  the buffer) — and the reset can wedge the port for ~90 s while the board restarts its print server —
  ``wedge_s``: the listening socket is closed that long, so connecting is refused, then reopened;
* a single stream was CUT at 7,229,016 bytes / 7½ min — ``cut_after_bytes``: a RST once a connection has
  accepted that many bytes;
* ``~S,CHECK`` answers ``00`` when ready — ``status_reply``; a status query is itself a connection.

``PROFILES["netps"]`` carries the measured numbers; ``PROFILES["ideal"]`` is a printer with none of the vices
(infinite drain, never resets, never cuts), for tests of the bytes alone.

    with FakePrinter(PROFILES["ideal"]) as fp:          # or FakePrinter(Profile(cut_after_bytes=100_000)).start()
        deliver(job, fp.target, media, offset)
        fp.jobs()[0] == offset_commands(media, offset) + job
        fp.decode(media)                                  # the pages, as rasterezpl decodes any job
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .media import EOL
from .stream import count_labels, decode_job

LINGER_RST = struct.pack(
    "hh" if sys.platform == "win32" else "ii", 1, 0
)  # SO_LINGER {on, 0 s}: close with a RST


@dataclass(frozen=True)
class Profile:
    """How the fake behaves; the defaults are the ideal printer."""

    name: str = "ideal"
    drain_bps: float | None = None  # bytes/s printed out of the buffer; None = instant (never busy)
    buffer_bytes: int = 1 << 20  # input buffer: reading stops while it is full
    reset_while_busy: bool = False  # a NEW connection while the buffer still drains is reset before any byte
    wedge_s: float = 0.0  # after such a reset: the port refuses connections this long (print server restart)
    cut_after_bytes: int | None = None  # RST once a connection has accepted this many bytes
    status_reply: bytes = b"00"  # the answer to ~S,CHECK

    def describe(self) -> str:
        parts = [
            f"drain {self.drain_bps:,.0f} B/s" if self.drain_bps else "instant drain",
            f"buffer {self.buffer_bytes:,} B",
            "resets a connection while busy" + (f" (port wedged {self.wedge_s:g} s)" if self.wedge_s else "")
            if self.reset_while_busy
            else "never resets",
            f"cuts after {self.cut_after_bytes:,} B" if self.cut_after_bytes else "never cuts",
            f"status {self.status_reply.decode('ascii', 'replace')!r}",
        ]
        return f"{self.name}: " + ", ".join(parts)


# the TDP43ME's NetPS board, 2026-10-16/17: 1.2 MB taken in 35 s–2 min then printed over minutes (~1.3 s per
# ~10 KB web row ≈ 8 KB/s), a 5 MB stream passed, 19.8 MB was cut at 7,229,016 bytes, a reset wedged the port ~90 s
NETPS = Profile(
    name="netps",
    drain_bps=8_000,
    buffer_bytes=4 << 20,
    reset_while_busy=True,
    wedge_s=90.0,
    cut_after_bytes=7_229_016,
)
PROFILES: dict[str, Profile] = {"ideal": Profile(), "netps": NETPS}


@dataclass
class Connection:
    """One TCP connection as the fake saw it."""

    n: int  # 1-based
    t_open: float
    t_close: float | None = None
    accepted: int = 0  # job bytes read
    data: bytearray = field(default_factory=bytearray)
    status: bool = False  # a ~S,CHECK query, answered
    reset: str | None = (
        None  # None (closed by the sender), "busy" (reset before any byte), "cut" (after accepted)
    )

    @property
    def seconds(self) -> float:
        return (self.t_close if self.t_close is not None else time.monotonic()) - self.t_open

    def describe(self) -> str:
        if self.reset == "busy":
            return f"#{self.n} RESET at once — a connection opened while the printer was busy"
        head = f"#{self.n} "
        if self.status:
            return head + "status query answered"
        try:
            labels = f"{count_labels(bytes(self.data))} blocks"
        except ValueError:
            labels = "cut inside a block"
        if self.reset == "cut":
            return head + f"CUT after {self.accepted:,} bytes ({labels})"
        return head + f"accepted {self.accepted:,} bytes in {self.seconds:.2f} s ({labels})"


class FakePrinter:
    """The fake on ``host:port`` (port 0 = any free one). ``start()`` listens in a daemon thread; ``stop()``
    closes everything. ``record_dir``: every job connection's bytes also land in ``job-<n>.ezpl`` there.
    ``on_event`` (a callable taking a string) hears each connection's verdict — the CLI prints them."""

    def __init__(
        self,
        profile: Profile | None = None,
        host: str = "127.0.0.1",
        port: int = 0,
        record_dir: str | Path | None = None,
        on_event: Any = None,
    ):
        self.profile = profile or Profile()
        self.host, self._port = host, port
        self.record_dir = Path(record_dir) if record_dir else None
        self.on_event = on_event
        self.connections: list[Connection] = []
        self._lock = threading.Lock()
        self._buffer = 0.0  # unprinted bytes
        self._t_drain = time.monotonic()
        self._stop = threading.Event()
        self._lsock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self.wedged_until = 0.0

    # -- lifecycle ---------------------------------------------------------------------------------------------
    def _listen(self) -> socket.socket:
        s = socket.socket()
        if sys.platform != "win32":
            s.setsockopt(
                socket.SOL_SOCKET, socket.SO_REUSEADDR, 1
            )  # rebinding after a wedge (TIME_WAIT peers)
        s.bind((self.host, self._port))
        s.listen(8)
        self._port = s.getsockname()[1]
        return s

    def start(self) -> FakePrinter:
        self._lsock = self._listen()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True, name="fakeprinter")
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._lsock is not None:
            try:
                self._lsock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)

    def __enter__(self) -> FakePrinter:
        return self.start() if self._thread is None else self

    def __exit__(self, *_exc) -> None:
        self.stop()

    @property
    def port(self) -> int:
        return self._port

    @property
    def target(self) -> str:
        return f"tcp://{self.host}:{self._port}"

    # -- the buffer model ------------------------------------------------------------------------------------------
    def _drain(self) -> None:
        now = time.monotonic()
        if self.profile.drain_bps is None:
            self._buffer = 0.0
        else:
            self._buffer = max(0.0, self._buffer - self.profile.drain_bps * (now - self._t_drain))
        self._t_drain = now

    def buffered(self) -> int:
        """Bytes accepted and not yet 'printed' — what the board would still be printing from its buffer."""
        with self._lock:
            self._drain()
            return int(self._buffer)

    @property
    def busy(self) -> bool:
        return self.buffered() > 0

    # -- connections -------------------------------------------------------------------------------------------
    def _event(self, text: str) -> None:
        if self.on_event is not None:
            self.on_event(text)

    def _accept_loop(self) -> None:
        assert self._lsock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._lsock.accept()
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with self._lock:
                c = Connection(len(self.connections) + 1, time.monotonic())
                self.connections.append(c)
            if self.profile.reset_while_busy and self.busy:
                c.reset, c.t_close = "busy", time.monotonic()
                _rst(conn)
                self._event(c.describe() + f" ({self.buffered():,} bytes still printing)")
                if self.profile.wedge_s > 0:  # the print server restarts: the port refuses meanwhile
                    self.wedged_until = time.monotonic() + self.profile.wedge_s
                    self._event(
                        f"   port {self._port} wedged for {self.profile.wedge_s:g} s (connections refused)"
                    )
                    self._lsock.close()
                    if self._stop.wait(self.profile.wedge_s):
                        return
                    self._lsock = self._listen()
                    self._event(f"   port {self._port} listening again")
                continue
            threading.Thread(target=self._serve, args=(conn, c), daemon=True).start()

    def _serve(self, conn: socket.socket, c: Connection) -> None:
        conn.settimeout(0.5)
        try:
            while not self._stop.is_set():
                with self._lock:
                    self._drain()
                    room = int(self.profile.buffer_bytes - self._buffer)
                if room <= 0:  # the buffer is full: the board reads nothing until the head frees room
                    time.sleep(0.005)
                    continue
                try:
                    chunk = conn.recv(min(65536, room))
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                if c.accepted == 0 and not c.data and chunk.startswith(b"~S"):
                    c.status = True
                    try:
                        conn.sendall(self.profile.status_reply + EOL)
                    except OSError:
                        pass
                    continue
                c.accepted += len(chunk)
                c.data += chunk
                with self._lock:
                    self._buffer += len(chunk)
                if self.profile.cut_after_bytes is not None and c.accepted >= self.profile.cut_after_bytes:
                    c.reset = "cut"
                    _rst(conn)
                    break
        finally:
            c.t_close = time.monotonic()
            try:
                conn.close()
            except OSError:
                pass
            if c.data and self.record_dir is not None:
                self.record_dir.mkdir(parents=True, exist_ok=True)
                (self.record_dir / f"job-{c.n}.ezpl").write_bytes(bytes(c.data))
            self._event(c.describe())

    # -- what it received --------------------------------------------------------------------------------------
    def jobs(self) -> list[bytes]:
        """The bytes of every connection that carried job data, in connection order (a cut job included, as
        far as it got)."""
        with self._lock:
            return [bytes(c.data) for c in self.connections if c.data]

    def received(self) -> bytes:
        return b"".join(self.jobs())

    def decode(self, m, job: int | None = None) -> list:
        """The pages of a job (``job`` = 1-based index; default: all jobs back to back) through the package's own
        decoder — what the roll would show. A job cut inside a block raises ValueError (truncated Q block)."""
        data = self.received() if job is None else self.jobs()[job - 1]
        return decode_job(data, m)

    def wait_accepted(self, n: int, timeout: float = 30.0) -> bool:
        """Until the fake has read ``n`` job bytes in all. A sender returns when the KERNEL took its bytes; the
        fake's thread reads them a moment later — a test that asks "busy?" right after a send must wait here."""
        t_end = time.monotonic() + timeout
        while sum(c.accepted for c in self.connections) < n:
            if time.monotonic() > t_end:
                return False
            time.sleep(0.005)
        return True

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Until the buffer has drained — the fake's END label in the hand."""
        t_end = time.monotonic() + timeout
        while self.busy:
            if time.monotonic() > t_end:
                return False
            time.sleep(0.01)
        return True


def _rst(conn: socket.socket) -> None:
    try:
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, LINGER_RST)
    except OSError:
        pass
    try:
        conn.close()
    except OSError:
        pass


def profile_from_args(
    base: str = "ideal",
    drain_bps: float | None = None,
    buffer_bytes: int | None = None,
    reset_while_busy: bool | None = None,
    wedge_s: float | None = None,
    cut_after_bytes: int | None = None,
    status: str | None = None,
) -> Profile:
    """A named profile with knobs overridden (the CLI's arguments)."""
    if base not in PROFILES:
        raise ValueError(f"unknown profile {base!r}: {', '.join(PROFILES)}")
    p = PROFILES[base]
    changes: dict[str, Any] = {}
    if drain_bps is not None:
        changes["drain_bps"] = None if drain_bps <= 0 else float(drain_bps)
    if buffer_bytes is not None:
        changes["buffer_bytes"] = int(buffer_bytes)
    if reset_while_busy is not None:
        changes["reset_while_busy"] = bool(reset_while_busy)
    if wedge_s is not None:
        changes["wedge_s"] = float(wedge_s)
    if cut_after_bytes is not None:
        changes["cut_after_bytes"] = None if cut_after_bytes <= 0 else int(cut_after_bytes)
    if status is not None:
        changes["status_reply"] = status.encode("ascii")
    return replace(p, **changes) if changes else p


def serve(profile: Profile, host: str = "127.0.0.1", port: int = 9100, record_dir: str | None = None) -> int:
    """``rasterezpl fakeprinter``: listen until Ctrl-C, a line per connection on stdout."""
    fp = FakePrinter(profile, host, port, record_dir, on_event=lambda s: print(s, flush=True))
    fp.start()
    print(f"fake printer listening on {fp.target} — {profile.describe()}", flush=True)
    if record_dir:
        print(f"recording every job to {record_dir}/job-<n>.ezpl", flush=True)
    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        fp.stop()
    return 0
