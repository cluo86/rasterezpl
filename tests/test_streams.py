"""printcart = ONE stream per printer (0.16.0): the files back to back over one connection, every file logged
with its place in the stream, a cut stream reported file by file. Fake ports and file spools only."""

from __future__ import annotations

import json
import socket
import threading

import pytest

import rasterezpl as rz
from rasterezpl import transport
from rasterezpl.cli import main
from rasterezpl.jobs import LOG_NAME, compose_streams, format_plan, plan, run
from rasterezpl.media import offset_commands
from rasterezpl.registry import load
from rasterezpl.stream import parse_blocks, select_labels

PIL = pytest.importorskip("PIL")

REG = """
media:
  big:
    dpi: 100
    width_mm: 80
    length_mm: 30
    gap_mm: 2
    darkness: 10
    areas_in: [[0.1, 0.05, 1.5, 1.0], [1.6, 0.05, 1.5, 1.0]]
  huge:
    dpi: 200
    width_mm: 170
    length_mm: 60
    gap_mm: 2
    areas_in: [[0.1, 0.1, 3.0, 2.0], [3.3, 0.1, 3.0, 2.0]]
printers:
  p1:
    transport: "{t1}"
    media: big
    offset_in: [0.1, -0.02]
    darkness: 12
  tcp:
    transport: "tcp://127.0.0.1:{port}"
    media: huge
"""


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(transport, "READY_BACKOFF_S", (0.05, 0.1, 0.2))


def _site(tmp_path, port=1, one_printer=False, ink=False):
    reg = tmp_path / "printers.yaml"
    t1 = tmp_path / "spool1.ezpl"
    text = REG.format(t1=t1, port=port)
    reg.write_text(text.split("  tcp:")[0] if one_printer else text)
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    m = r.media["huge" if ink else "big"]
    img = None
    if ink:  # black 600 × 400 areas: ~60 KB per web row, so a 66-row file outgrows every socket buffer
        from PIL import Image

        _, _, aw, ah = m.area_px(0)
        img = Image.new("L", (aw, ah), 0)
    for name, n in (("two.ezpl", 2), ("b30.ezpl", 66), ("b31.ezpl", 66)):
        (root / name).write_bytes(rz.job(m, [[img, img]] * n))
    return {"reg": r, "regpath": reg, "root": root, "t1": t1}


def test_one_stream_per_printer_with_the_files_back_to_back(tmp_path):
    site = _site(tmp_path)
    root, reg = site["root"], site["reg"]
    m = reg.media["big"]
    jobs = [{"file": f, "printer": "p1"} for f in ("two.ezpl", "b30.ezpl", "b31.ezpl")]
    rows = plan(root, reg, jobs)
    assert [r.rows for r in rows] == [2, 66, 66]
    streams = compose_streams(root, reg, rows, bookends=True)
    assert len(streams) == 1
    st = streams[0]
    assert st.printer == "p1" and st.blocks == 2 + 66 + 66 + 6 and [p.rows for p in st.parts] == [4, 68, 68]
    prefix = offset_commands(m, (0.1, -0.02))
    assert st.prefix == prefix and st.data.startswith(prefix + b"^Q30,2\r^W80\r~R200\r^AT\r^H12\r")
    # file offsets = where each part's bytes sit in the stream as sent
    assert [p.offset for p in st.parts] == [
        len(prefix),
        len(prefix) + st.parts[0].length,
        len(prefix) + st.parts[0].length + st.parts[1].length,
    ]
    data = st.data
    for p in st.parts:
        assert data[p.offset : p.end] == p.data
    assert st.length == len(data) == len(prefix) + sum(p.length for p in st.parts)
    # the heat is retuned once, in front: the second file's own setup keeps the file's ^H10
    assert parse_blocks(st.parts[1].data)[0][0].endswith(b"^H10\r^P1\r^L\r")
    # the plan and the dry run say so, nothing is written
    txt = format_plan(rows, bookends=True)
    assert "268 labels in 3 files → 268 on p1 (3 files, one stream) (+2 bookends per file)" in txt
    notes = run(root, reg, rows, root / LOG_NAME, dry_run=True, bookends=True)
    assert (
        notes[0].startswith(f"stream {st.id[:0]}") and " → p1: 3 files, 140 blocks (6 bookends), " in notes[0]
    )
    assert (
        "two.ezpl labels 1-4 (2 rows) · b30.ezpl labels 1-132 (66 rows) · b31.ezpl labels 1-132 (66 rows)"
        in notes[0]
    )
    assert notes[-1] == "dry run — +2 bookends per file, nothing sent" and not site["t1"].exists()
    assert not (root / LOG_NAME).exists()
    # the real thing: one write, three log lines whose offsets match the stream
    notes = run(root, reg, rows, root / LOG_NAME, bookends=True)
    spool = site["t1"].read_bytes()
    assert rz.count_labels(spool) == 140 and len(spool) == st.length
    entries = [json.loads(ln) for ln in (root / LOG_NAME).read_text().splitlines()]
    assert [e["file"] for e in entries] == ["two.ezpl", "b30.ezpl", "b31.ezpl"]
    assert [e["ok"] for e in entries] == [True, True, True]
    assert len({e["stream"]["id"] for e in entries}) == 1
    assert [(e["stream"]["offset"], e["stream"]["length"]) for e in entries] == [
        (p.offset, p.length) for p in st.parts
    ]
    assert [e["bytes_sent"] for e in entries] == [p.length for p in st.parts]
    for e in entries:
        assert (
            spool[e["stream"]["offset"] : e["stream"]["offset"] + e["stream"]["length"]]
            == data[e["stream"]["offset"] : e["stream"]["offset"] + e["stream"]["length"]]
        )
    assert [r.sent for r in rows] == [True, True, True] and rows[1].stream == entries[1]["stream"]
    assert (
        notes[1].startswith("stream ")
        and " → p1: wrote " in notes[1]
        and "(darkness 12, speed file)" in notes[1]
    )
    assert notes[2].startswith("two.ezpl labels 1-4 +2 bookends → sent in stream ")


def test_a_stream_cut_inside_file_two(tmp_path):
    got = bytearray()
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    site = _site(tmp_path, port, ink=True)
    root, reg = site["root"], site["reg"]
    jobs = [{"file": f, "printer": "tcp"} for f in ("two.ezpl", "b30.ezpl", "b31.ezpl")]
    rows = plan(root, reg, jobs)
    st = compose_streams(root, reg, rows)[0]
    K = st.parts[1].offset + st.parts[1].length // 2  # inside b30.ezpl, about its middle

    def close_after_k():
        conn, _ = srv.accept()
        with conn:
            while len(got) < K:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                got.extend(chunk)
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")

    threading.Thread(target=close_after_k, daemon=True).start()
    notes = run(root, reg, rows, root / LOG_NAME)
    entries = [json.loads(ln) for ln in (root / LOG_NAME).read_text().splitlines()]
    assert [e["file"] for e in entries] == ["two.ezpl", "b30.ezpl", "b31.ezpl"]
    assert [e["ok"] for e in entries] == [True, False, False]
    accepted = entries[0]["bytes_sent"] + entries[1]["bytes_sent"]  # file 1 whole, file 2 up to the cut
    assert (
        entries[0]["bytes_sent"] == st.parts[0].length and 0 < entries[1]["bytes_sent"] < st.parts[1].length
    )
    assert entries[2]["bytes_sent"] == 0 and entries[2]["note"] == "not sent"
    assert entries[2]["error"].startswith("not sent: stream ") and "was cut before it" in entries[2]["error"]
    # the cut row: the block holding the first byte the printer did not take, named with the file's row count
    cut = entries[1]["error"]
    assert cut.startswith("cut inside b30.ezpl at web row ") and cut.endswith(" of 66")
    row = int(cut.split("web row ")[1].split(" ")[0])
    pos, k = st.parts[1].offset, 0
    for setup, qs in parse_blocks(st.parts[1].data):
        k += 1
        pos += len(rz.serialise_block(setup, qs))
        if len(st.prefix) + accepted < pos:
            break
    assert row == k and 1 <= row <= 66
    assert entries[1]["note"].startswith("CUT — labels ARE on the paper up to the cut (")
    assert "connection closed by the printer after" in entries[1]["note"]
    assert [r.sent for r in rows] == [True, False, False] and rows[1].error == cut
    # the notes: the stream's failure, then every file's verdict with the resume range for the cut one
    assert notes[0].startswith("stream ") and " → tcp: 3 files, 134 blocks, " in notes[0]
    assert notes[1].startswith("stream ") and "FAILED after " in notes[1] and " bytes of " in notes[1]
    assert notes[2].startswith("two.ezpl labels 1-4 → sent in stream ")
    first = (row - 1) * 2 + 1
    assert f"b30.ezpl labels 1-132 → CUT: {cut}; resume with --labels {first}-132 — your call" in notes[3]
    assert "nothing is resent here" in notes[3]
    assert notes[4] == "b31.ezpl labels 1-132 → NOT SENT (the stream was cut before it)"
    assert len(notes) == 5 and bytes(got) == st.data[: len(got)]
    srv.close()


def test_cli_printcart_streams_dry_run_proof_and_exit_code(tmp_path, capsys):
    site = _site(tmp_path, one_printer=True)
    root = site["root"]
    argv = ["--registry", str(site["regpath"]), "printcart", "--root", str(root), "two.ezpl", "b30.ezpl:1-10"]
    pdf = tmp_path / "cart.pdf"
    assert main([*argv, "--dry-run", "--bookends", "--proof", str(pdf)]) == 0
    out = capsys.readouterr().out
    assert "14 labels in 2 files → 14 on p1 (2 files, one stream) (+2 bookends per file)" in out
    assert "2 files, 11 blocks (4 bookends)" in out and "proof: " in out and "(11 web rows" in out
    assert "dry run — +2 bookends per file, nothing sent" in out and not site["t1"].exists()
    assert pdf.read_bytes().startswith(b"%PDF")
    assert main(argv) == 0
    spool = site["t1"].read_bytes()
    assert rz.count_labels(spool) == 7  # 2 rows + 5 rows, no bookends
    entries = [json.loads(ln) for ln in (root / LOG_NAME).read_text().splitlines()]
    assert [(e["file"], e["labels"], e["count"], e["ok"]) for e in entries] == [
        ("two.ezpl", "1-4", 4, True),
        ("b30.ezpl", "1-10", 10, True),
    ]


def test_single_file_cart_and_print_are_byte_identical_to_0_14_0(tmp_path):
    """Regression pin: one file = the registration prefix + the selection, exactly as 0.14.0 sent it — and the
    first file of a stream is that same byte string."""
    site = _site(tmp_path)
    root, reg = site["root"], site["reg"]
    m = reg.media["big"]
    expected = offset_commands(m, (0.1, -0.02)) + rz.retune(
        select_labels((root / "two.ezpl").read_bytes(), "1-3", m), 12, None
    )
    rows = plan(root, reg, [{"file": "two.ezpl", "labels": "1-3", "printer": "p1"}])
    run(root, reg, rows, root / LOG_NAME)
    assert site["t1"].read_bytes() == expected
    site["t1"].unlink()
    assert (
        main(
            [
                "--registry",
                str(site["regpath"]),
                "print",
                str(root / "two.ezpl"),
                "--printer",
                "p1",
                "--labels",
                "1-3",
            ]
        )
        == 0
    )
    assert site["t1"].read_bytes() == expected
    rows = plan(
        root,
        reg,
        [{"file": "two.ezpl", "labels": "1-3", "printer": "p1"}, {"file": "b31.ezpl", "printer": "p1"}],
    )
    run(root, reg, rows, root / LOG_NAME)
    assert site["t1"].read_bytes().startswith(expected)


def test_an_old_settle_record_is_ignored_with_a_note(tmp_path, capsys):
    site = _site(tmp_path, one_printer=True)
    reg = site["regpath"]
    reg.write_text(
        reg.read_text().replace(
            "    darkness: 12\n", "    darkness: 12\n    settle:\n      per_row_s: 1.5\n      floor_s: 10\n"
        )
    )
    assert (
        main(["--registry", str(reg), "printcart", "--root", str(site["root"]), "two.ezpl:1", "--dry-run"])
        == 0
    )
    out = capsys.readouterr().out
    assert "note: p1's `settle` record is ignored since 0.16.0 — a cart is one stream per printer" in out
