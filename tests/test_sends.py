"""Sends that survive an Ethernet board (0.14.0): a fake print port that refuses, resets or closes mid-job —
never a real printer. Values are asserted: the bytes named in the error are the bytes in the log line."""

from __future__ import annotations

import json
import re
import socket
import threading
import time

import pytest

import rasterezpl as rz
from rasterezpl import transport
from rasterezpl.cli import main
from rasterezpl.jobs import LOG_NAME, plan, run, send_logged
from rasterezpl.media import offset_commands
from rasterezpl.registry import load
from rasterezpl.stream import select_labels
from rasterezpl.transport import TCP_CHUNK, SendError, send_tcp

PIL = pytest.importorskip("PIL")

REG = """
media:
  tiny:
    dpi: 100
    width_mm: 20
    length_mm: 10
    gap_mm: 2
    areas_in: [[0, 0, 0.16, 0.08], [0.4, 0.1, 0.16, 0.08]]
printers:
  filep:
    transport: "{t1}"
    media: tiny
    offset_in: [0.1, -0.02]
  filep2:
    transport: "{t2}"
    media: tiny
"""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(transport, "READY_BACKOFF_S", (0.05, 0.1, 0.2))


def _port_server(behaviour):
    """A listening socket whose accept loop runs `behaviour(conn, n)` for the n-th connection (0-based)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)

    def run_():
        n = 0
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            with conn:
                behaviour(conn, n)
            n += 1

    threading.Thread(target=run_, daemon=True).start()
    return srv, srv.getsockname()[1]


def _read_all(conn, received: bytearray):
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            return
        received.extend(chunk)


def test_connection_closed_after_k_bytes_names_the_bytes_and_logs_them(tmp_path):
    got = bytearray()
    K = 3000

    def close_after_k(conn, n):
        while len(got) < K:
            chunk = conn.recv(1024)
            if not chunk:
                return
            got.extend(chunk)
        conn.setsockopt(
            socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00"
        )  # RST, not FIN

    srv, port = _port_server(close_after_k)
    data = b"x" * (TCP_CHUNK * 128)  # 8 MB: beyond any loopback socket buffer, so the cut is seen
    with pytest.raises(SendError) as ei:
        send_tcp(data, "127.0.0.1", port, timeout_s=2.0, deadline_s=5.0, ready_s=1.0)
    e = ei.value
    assert e.bytes_sent >= K and e.bytes_sent < len(data)
    assert str(e) == str(e).replace("\n", "") and re.search(
        rf"127\.0\.0\.1:{port}: connection closed by the printer after {e.bytes_sent} bytes", str(e)
    )
    # through the logged send path: ok false, bytes_sent = the same number, error = the message
    log = tmp_path / "log.jsonl"
    got.clear()
    ok, note = send_logged(
        data,
        f"tcp://127.0.0.1:{port}",
        log,
        file="j.ezpl",
        labels="1-8",
        count=8,
        bookends=False,
        printer="p",
        media=None,
    )
    entry = json.loads(log.read_text().splitlines()[-1])
    assert ok is False and entry["ok"] is False and entry["bytes_sent"] >= K
    assert (
        entry["error"].endswith(
            f"after {entry['bytes_sent']} bytes (ConnectionResetError: [Errno 54] Connection reset by peer)"
        )
        or f"after {entry['bytes_sent']} bytes" in entry["error"]
    )
    assert (
        entry["note"]
        == f"FAILED after {entry['bytes_sent']} bytes — labels ARE on the paper up to where the stream stopped"
    )
    assert note.startswith("FAILED: ") and "resume with --labels N-M" in note
    assert [entry[k] for k in ("file", "labels", "count", "printer", "transport", "bookends")] == [
        "j.ezpl",
        "1-8",
        8,
        "p",
        f"tcp://127.0.0.1:{port}",
        False,
    ]
    srv.close()


def test_reset_before_the_first_byte_is_retried_then_succeeds():
    got = bytearray()

    def first_closes(conn, n):
        if n == 0:
            return  # closed at once: the board that resets a connection opened while it prints
        _read_all(conn, got)

    srv, port = _port_server(first_closes)
    data = b"y" * 5000
    t0 = time.monotonic()
    assert send_tcp(data, "127.0.0.1", port, timeout_s=2.0, deadline_s=5.0, ready_s=5.0, probe_s=0.2) == 5000
    time.sleep(0.2)
    assert bytes(got) == data and time.monotonic() - t0 < 4.0
    srv.close()


def test_a_refused_port_is_waited_on_not_failed():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here for a moment: connection refused
    got = bytearray()
    holder = {}

    def listen_later():
        time.sleep(0.6)
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        holder["srv"] = srv
        conn, _ = srv.accept()
        with conn:
            _read_all(conn, got)

    threading.Thread(target=listen_later, daemon=True).start()
    assert send_tcp(b"z" * 700, "127.0.0.1", port, timeout_s=2.0, deadline_s=5.0, ready_s=5.0) == 700
    time.sleep(0.2)
    assert bytes(got) == b"z" * 700
    # and at the deadline it is a SendError with 0 bytes: nothing on the paper
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead = probe.getsockname()[1]
    probe.close()
    with pytest.raises(SendError) as ei:
        send_tcp(b"q", "127.0.0.1", dead, timeout_s=1.0, deadline_s=1.0, ready_s=0.3)
    assert ei.value.bytes_sent == 0 and "not ready" in str(ei.value) and "nothing sent" in str(ei.value)


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    t1, t2 = tmp_path / "spool1.ezpl", tmp_path / "spool2.ezpl"
    reg.write_text(REG.format(t1=t1, t2=t2))
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None], [None, None]]))  # 4 labels
    return {"reg": r, "regpath": reg, "root": root, "t1": t1, "t2": t2}


def test_print_writes_a_log_line_beside_the_job(site, capsys):
    root = site["root"]
    assert (
        main(
            [
                "--registry",
                str(site["regpath"]),
                "print",
                str(root / "a.ezpl"),
                "--printer",
                "filep",
                "--labels",
                "1-3",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    log = root / LOG_NAME
    assert f"logged in {log}" in out and log.exists()
    e = json.loads(log.read_text().splitlines()[-1])
    assert [e[k] for k in ("file", "labels", "count", "printer", "transport", "bookends", "ok")] == [
        str(root / "a.ezpl"),
        "1-3",
        3,
        "filep",
        str(site["t1"]),
        False,
        True,
    ]
    assert e["bytes_sent"] == len(site["t1"].read_bytes()) and e["note"].startswith("wrote ")
    # --log elsewhere, the whole file = labels 1-4
    other = site["root"].parent / "elsewhere.jsonl"
    assert (
        main(
            [
                "--registry",
                str(site["regpath"]),
                "print",
                str(root / "a.ezpl"),
                "--printer",
                "filep",
                "--log",
                str(other),
            ]
        )
        == 0
    )
    assert json.loads(other.read_text().splitlines()[-1])["labels"] == "1-4"
    # a failed send: exit 1, logged with ok false, the note names what is (not) on the paper
    bad = root / "sub"  # a spool path whose parent is a FILE → the write fails
    bad.write_text("x")
    rc = main(
        [
            "--registry",
            str(site["regpath"]),
            "print",
            str(root / "a.ezpl"),
            "--to",
            str(bad / "spool.ezpl"),
            "--media",
            "tiny",
            "--log",
            str(other),
        ]
    )
    assert rc == 1
    e = json.loads(other.read_text().splitlines()[-1])
    assert (
        e["ok"] is False
        and e["bytes_sent"] == 0
        and "error" in e
        and e["note"].startswith("FAILED — nothing on the paper")
    )


def test_without_new_options_the_bytes_are_identical_to_0_13_0(site):
    root, reg = site["root"], site["reg"]
    m = reg.media["tiny"]
    rows = plan(root, reg, [{"file": "a.ezpl", "labels": "1-3", "printer": "filep"}])
    run(root, reg, rows, root / LOG_NAME)
    expected = offset_commands(m, (0.1, -0.02)) + select_labels((root / "a.ezpl").read_bytes(), "1-3", m)
    assert site["t1"].read_bytes() == expected
    site["t1"].unlink()
    assert (
        main(
            [
                "--registry",
                str(site["regpath"]),
                "print",
                str(root / "a.ezpl"),
                "--printer",
                "filep",
                "--labels",
                "1-3",
            ]
        )
        == 0
    )
    assert site["t1"].read_bytes() == expected
