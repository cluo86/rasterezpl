"""The fake NetPS printer (0.22.0): the board's vices on demand, on a port — and the sender's lessons (0.14.0,
0.16.0) pinned against it instead of hand-written sockets: the stall rides, a reset before the first byte is
retried, a wedge is waited out, a cut names the bytes and the resume row never lies ahead of the paper."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time

import pytest

import rasterezpl as rz
from rasterezpl import transport
from rasterezpl.cli import main
from rasterezpl.fakeprinter import NETPS, PROFILES, profile_from_args
from rasterezpl.jobs import LOG_NAME, compose_streams, plan, run
from rasterezpl.media import offset_commands
from rasterezpl.registry import load
from rasterezpl.stream import parse_blocks, serialise_block
from rasterezpl.transport import SendError, deliver, send_tcp, status

PIL = pytest.importorskip("PIL")

REG = """
media:
  huge:
    dpi: 200
    width_mm: 170
    length_mm: 60
    gap_mm: 2
    areas_in: [[0.1, 0.1, 3.0, 2.0], [3.3, 0.1, 3.0, 2.0]]
printers:
  fake:
    transport: "{target}"
    media: huge
    offset_in: [0.05, -0.01]
"""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(transport, "READY_BACKOFF_S", (0.05, 0.1, 0.2))


def _ink_job(m, rows: int):
    """Black areas: ~60 KB per web row, so a stream outgrows every socket buffer."""
    from PIL import Image

    _, _, aw, ah = m.area_px(0)
    img = Image.new("L", (aw, ah), 0)
    return rz.job(m, [[img, img]] * rows)


def test_ideal_printer_takes_a_job_and_decodes_it_like_the_roll(fake_printer):
    fp = fake_printer()
    m = rz.PANDUIT_S150X225VATY_2UP
    from PIL import Image

    _, _, aw, ah = m.area_px(0)
    img = Image.new("L", (aw, ah), 255)
    img.paste(0, (10, 10, 60, 40))
    data = rz.job(m, [[img, None], [None, img]])
    note, n = deliver(data, fp.target, m, (0.01, 0.0))
    assert n == len(offset_commands(m, (0.01, 0.0)) + data) and f"sent {n} bytes (2 labels)" in note
    time.sleep(0.1)
    assert fp.jobs() == [offset_commands(m, (0.01, 0.0)) + data]
    pages = fp.decode(m)
    assert [p.tobytes() for p in pages] == [p.tobytes() for p in rz.decode_job(data, m)]
    assert fp.connections[0].accepted == n and fp.connections[0].reset is None and not fp.busy
    assert (
        fp.connections[0].describe().startswith("#1 accepted ")
        and "(2 blocks)" in fp.connections[0].describe()
    )


def test_status_query_is_answered_and_is_a_connection_of_its_own(fake_printer):
    fp = fake_printer()
    assert status(fp.target) == "00"
    paused = fake_printer(status_reply=b"01")
    assert status(paused.target) == "01"
    time.sleep(0.05)
    assert fp.connections[0].status is True and fp.connections[0].accepted == 0 and not fp.jobs()
    assert fp.connections[0].describe() == "#1 status query answered"


def test_the_buffer_and_the_drain_make_accepted_not_printed(fake_printer):
    """A 16 MB job into a 64 KB buffer draining at 32 MB/s: the send stalls on the board's pace (~0.5 s; the
    kernel's loopback buffers hide a few hundred KB of it) and completes; right after the send returns the fake
    is still printing from its buffer."""
    fp = fake_printer(buffer_bytes=65_536, drain_bps=32_000_000)
    data = b"\x00" * (16 << 20)
    t0 = time.monotonic()
    assert send_tcp(data, "127.0.0.1", fp.port, timeout_s=5.0, deadline_s=30.0) == len(data)
    took = time.monotonic() - t0
    assert took > 0.25, took  # the stall was real: 16 MB at 32 MB/s, minus what the kernel buffers absorbed
    assert fp.wait_accepted(len(data), 5.0) and fp.buffered() <= 65_536  # never more than the buffer in hand
    assert fp.wait_idle(5.0) and not fp.busy
    assert fp.received() == data


def test_a_connection_while_busy_is_reset_before_any_byte_and_the_sender_waits(fake_printer):
    fp = fake_printer(buffer_bytes=1 << 20, drain_bps=100_000, reset_while_busy=True)
    first = b"a" * 200_000  # two seconds' worth of printing
    assert send_tcp(first, "127.0.0.1", fp.port, timeout_s=5.0, deadline_s=30.0) == len(first)
    assert fp.wait_accepted(len(first), 5.0) and fp.busy  # the sender is done; the fake has it all and prints
    second = b"b" * 1000
    t0 = time.monotonic()
    assert (
        send_tcp(second, "127.0.0.1", fp.port, timeout_s=5.0, deadline_s=30.0, ready_s=10.0, probe_s=0.2)
        == 1000
    )
    assert time.monotonic() - t0 > 0.3  # it waited for the board, retrying
    time.sleep(0.1)
    resets = [c for c in fp.connections if c.reset == "busy"]
    assert resets and all(c.accepted == 0 for c in resets)
    assert fp.jobs() == [first, second]  # once each: nothing printed twice


def test_a_wedged_port_refuses_connections_and_the_sender_waits_it_out(fake_printer):
    fp = fake_printer(buffer_bytes=1 << 20, drain_bps=100_000, reset_while_busy=True, wedge_s=0.6)
    assert send_tcp(b"a" * 300_000, "127.0.0.1", fp.port, timeout_s=5.0, deadline_s=30.0) == 300_000
    assert fp.wait_accepted(300_000, 5.0) and fp.busy  # three seconds of printing ahead
    # a raw connection while busy: reset at once, and the port goes down for the wedge
    s = socket.create_connection(("127.0.0.1", fp.port), timeout=2.0)
    s.settimeout(1.0)
    try:
        assert s.recv(1) == b"" or True  # RST → b"" or an error; either way the peer is gone
    except OSError:
        pass
    s.close()
    time.sleep(0.1)
    with pytest.raises(OSError):  # refused while the print server "restarts"
        socket.create_connection(("127.0.0.1", fp.port), timeout=0.5).close()
    t0 = time.monotonic()
    assert send_tcp(b"c" * 10, "127.0.0.1", fp.port, timeout_s=5.0, deadline_s=30.0, ready_s=10.0) == 10
    assert 0.3 < time.monotonic() - t0 < 8.0
    time.sleep(0.1)
    assert fp.jobs()[-1] == b"c" * 10 and any(c.reset == "busy" for c in fp.connections)
    # and the deadline is honoured: a port that stays wedged is "not ready", 0 bytes
    fp2 = fake_printer(buffer_bytes=1 << 20, drain_bps=100, reset_while_busy=True, wedge_s=30.0)
    send_tcp(b"a" * 1000, "127.0.0.1", fp2.port, timeout_s=5.0, deadline_s=30.0)
    assert fp2.wait_accepted(1000, 5.0)
    with pytest.raises(SendError) as ei:
        send_tcp(b"b", "127.0.0.1", fp2.port, timeout_s=1.0, deadline_s=5.0, ready_s=0.6, probe_s=0.1)
    assert ei.value.bytes_sent == 0 and "nothing sent" in str(ei.value)


def test_a_cut_names_the_bytes_and_the_fake_kept_what_it_accepted(fake_printer):
    K = 150_000
    fp = fake_printer(cut_after_bytes=K)
    data = bytes(range(256)) * 4000  # 1 MB
    with pytest.raises(SendError) as ei:
        send_tcp(data, "127.0.0.1", fp.port, timeout_s=5.0, deadline_s=30.0, ready_s=1.0)
    e = ei.value
    time.sleep(0.1)
    c = fp.connections[0]
    assert c.reset == "cut" and c.accepted >= K
    assert e.bytes_sent >= c.accepted  # what the fake read, the sender had sent — never the other way round
    assert f"connection closed by the printer after {e.bytes_sent} bytes" in str(e)
    assert fp.jobs()[0] == data[: c.accepted] and c.describe().startswith(
        f"#1 CUT after {c.accepted:,} bytes"
    )


def test_printcart_cut_inside_file_two_reports_a_row_the_roll_can_confirm(fake_printer, tmp_path):
    """The resume row printcart names is computed from the bytes the SENDER handed over; the fake knows what it
    accepted. The reported row must never lie BEFORE the paper's last complete row — resuming from it would skip
    labels; later is the operator's call ("your call from what the roll shows")."""
    reg = tmp_path / "printers.yaml"
    root = tmp_path / "root"
    root.mkdir()
    r0 = load(None)
    m = rz.registry.media_from_dict(
        "huge",
        {
            "dpi": 200,
            "width_mm": 170,
            "length_mm": 60,
            "gap_mm": 2,
            "areas_in": [[0.1, 0.1, 3.0, 2.0], [3.3, 0.1, 3.0, 2.0]],
        },
    )
    del r0
    for name, n in (("two.ezpl", 2), ("b30.ezpl", 40), ("b31.ezpl", 40)):
        (root / name).write_bytes(_ink_job(m, n))
    probe = load(str(tmp_path / "x.yaml")) if False else None
    del probe
    # the cut: inside b30, about its middle, measured on the composed stream
    reg.write_text(REG.format(target="tcp://127.0.0.1:1"), encoding="utf-8")
    rows = plan(
        root, load(str(reg)), [{"file": f, "printer": "fake"} for f in ("two.ezpl", "b30.ezpl", "b31.ezpl")]
    )
    st = compose_streams(root, load(str(reg)), rows)[0]
    K = st.parts[1].offset + st.parts[1].length // 2
    fp = fake_printer(cut_after_bytes=K)
    reg.write_text(REG.format(target=fp.target), encoding="utf-8")
    r = load(str(reg))
    rows = plan(root, r, [{"file": f, "printer": "fake"} for f in ("two.ezpl", "b30.ezpl", "b31.ezpl")])
    notes = run(root, r, rows, root / LOG_NAME)
    time.sleep(0.1)
    entries = [json.loads(ln) for ln in (root / LOG_NAME).read_text(encoding="utf-8").splitlines()]
    assert [e["ok"] for e in entries] == [True, False, False]
    cut = entries[1]["error"]
    assert cut.startswith("cut inside b30.ezpl at web row ") and cut.endswith(" of 40")
    row = int(cut.split("web row ")[1].split(" ")[0])
    # what the fake accepted of b30: the complete blocks it has are on the paper
    accepted = fp.connections[0].accepted
    part = st.parts[1]
    got = fp.received()[part.offset : min(accepted, part.end)]
    complete = 0
    pos = 0
    for setup, qs in parse_blocks(part.data):
        pos += len(serialise_block(setup, qs))
        if pos <= len(got):
            complete += 1
    assert row >= complete + 1, (row, complete)  # never before the paper
    assert any("resume with --labels" in n and f"{(row - 1) * 2 + 1}-80" in n for n in notes)
    assert entries[2]["note"] == "not sent"


def test_profiles_and_cli_knobs():
    assert PROFILES["netps"] is NETPS
    assert (
        NETPS.drain_bps,
        NETPS.buffer_bytes,
        NETPS.reset_while_busy,
        NETPS.wedge_s,
        NETPS.cut_after_bytes,
    ) == (
        8_000,
        4 << 20,
        True,
        90.0,
        7_229_016,
    )
    assert "cuts after 7,229,016 B" in NETPS.describe() and "port wedged 90 s" in NETPS.describe()
    p = profile_from_args("netps", drain_bps=0, cut_after_bytes=0, wedge_s=1.5, status="01")
    assert p.drain_bps is None and p.cut_after_bytes is None and p.wedge_s == 1.5 and p.status_reply == b"01"
    assert p.reset_while_busy is True and p.name == "netps"
    assert profile_from_args("ideal") is PROFILES["ideal"]
    with pytest.raises(ValueError, match="unknown profile"):
        profile_from_args("zebra")
    with pytest.raises(SystemExit):
        main(["fakeprinter", "--profile", "zebra"])


def test_cli_fakeprinter_end_to_end(tmp_path):
    """`rasterezpl fakeprinter --port 0 --record DIR` in a subprocess, a print through the CLI to it, the recorded
    job byte for byte — the smoke test CI runs on every OS with no hardware."""
    rec = tmp_path / "rec"
    proc = subprocess.Popen(
        [sys.executable, "-m", "rasterezpl", "fakeprinter", "--port", "0", "--record", str(rec)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        line = proc.stdout.readline()
        assert line.startswith("fake printer listening on tcp://127.0.0.1:"), line
        target = line.split(" on ")[1].split(" ")[0]
        assert "ideal:" in line and "never cuts" in line
        assert proc.stdout.readline().startswith("recording every job to ")
        m = rz.PANDUIT_S150X225VATY_2UP
        job = tmp_path / "j.ezpl"
        job.write_bytes(rz.job(m, [[None, None]] * 3))
        rc = main(
            ["print", str(job), "--to", target, "--media", "panduit-s150x225vaty-2up", "--labels", "1-4"]
        )
        assert rc == 0
        verdict = proc.stdout.readline()
        assert verdict.startswith("#1 accepted ") and "(2 blocks)" in verdict, verdict
        recorded = (rec / "job-1.ezpl").read_bytes()
        assert recorded == b"^R0\r~Q+0\r" + rz.select_labels(job.read_bytes(), "1-4", m)
        assert status(target) == "00"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
