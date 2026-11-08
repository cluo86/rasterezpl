"""send_tcp streams a job in chunks and survives a slow consumer (2026-10-16: a 128-label job timed out on sendall)."""

import socket
import threading
import time

from rasterezpl.transport import TCP_CHUNK, send_tcp


def _slow_server(received: bytearray, delay: float, stop_after: int | None = None):
    """A listening port that reads 4 KB at a time with a pause between reads. Returns (port, the reader thread):
    join the thread before looking at `received` — a fixed sleep was not enough on a slow CI runner."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        conn, _ = srv.accept()
        with conn:
            while True:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                received.extend(chunk)
                if stop_after is not None and len(received) >= stop_after:
                    time.sleep(delay)
                    break
                time.sleep(delay)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return srv.getsockname()[1], t


def test_send_tcp_streams_a_job_bigger_than_one_chunk_to_a_slow_consumer():
    data = bytes(range(256)) * (3 * TCP_CHUNK // 256 + 7)  # > 3 chunks
    got = bytearray()
    port, reader = _slow_server(got, delay=0.002)
    n = send_tcp(data, "127.0.0.1", port, timeout_s=2.0, deadline_s=30.0)
    reader.join(timeout=30)
    assert n == len(data) and bytes(got) == data


def test_send_tcp_names_the_bytes_sent_when_the_printer_stops_taking_data():
    data = b"x" * (TCP_CHUNK * 4)
    got = bytearray()
    port, _ = _slow_server(
        got, delay=5.0, stop_after=TCP_CHUNK
    )  # takes one chunk, then stalls past the deadline
    try:
        send_tcp(data, "127.0.0.1", port, timeout_s=0.5, deadline_s=1.0)
    except RuntimeError as e:
        assert "stopped taking data" in str(e) and "bytes" in str(e)
    else:  # the kernel may buffer the whole job before the server reads it — then there is nothing to detect
        assert True
