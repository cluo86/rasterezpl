"""Bookends (0.13.0): a START and an END block around a job, in the same send — file spools only, no printer."""

from __future__ import annotations

import json
import re

import pytest

import rasterezpl as rz
from rasterezpl import bookends as bk
from rasterezpl.cli import main
from rasterezpl.jobs import LOG_NAME, format_plan, plan, run
from rasterezpl.media import offset_commands
from rasterezpl.registry import load
from rasterezpl.stream import decode_block, parse_blocks, select_labels

PIL = pytest.importorskip("PIL")

# a two-across media large enough for the marks (150 × 100 dots per area); `tiny` (16 × 8 dots) is below that
REG = """
media:
  big:
    dpi: 100
    width_mm: 80
    length_mm: 30
    gap_mm: 2
    darkness: 10
    areas_in: [[0.1, 0.05, 1.5, 1.0], [1.6, 0.05, 1.5, 1.0]]
  tiny:
    dpi: 100
    width_mm: 20
    length_mm: 10
    gap_mm: 2
    areas_in: [[0, 0, 0.16, 0.08], [0.4, 0.1, 0.16, 0.08]]
printers:
  p1:
    transport: "{t1}"
    media: big
    offset_in: [0.1, -0.02]
  p2:
    transport: "{t2}"
    media: big
    darkness: 12
  small:
    transport: "{t3}"
    media: tiny
"""
TS = "2026-10-16 14:05"


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    t = [tmp_path / f"spool{i}.ezpl" for i in (1, 2, 3)]
    reg.write_text(REG.format(t1=t[0], t2=t[1], t3=t[2]))
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.ezpl").write_bytes(rz.job(r.media["big"], [[None, None]] * 3))  # 6 labels, 3 blocks
    (root / "b.ezpl").write_bytes(rz.job(r.media["big"], [[None, None]] * 2))  # 4 labels, 2 blocks
    (root / "t.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None]]))
    return {"reg": r, "regpath": reg, "root": root, "t": t}


def _ink(block, m) -> float:
    """The black fraction of a block's page — a bookend is mostly ink, a blank label none."""
    page = decode_block(block[1], m)
    hist = page.histogram()
    return hist[0] / (page.size[0] * page.size[1])


def test_lines_carry_the_selection_the_total_and_the_count():
    assert bk.start_lines("x/labels-mtp-B19.ezpl", "97-128", 128, 32, "tdp43me", TS) == [
        "labels-mtp-B19",
        "labels 97-128 of 128",
        "32 labels · tdp43me · 2026-10-16 14:05",
    ]
    assert bk.end_lines("labels-mtp-B19.ezpl", 32, TS) == [
        "labels-mtp-B19",
        "32/32 sent · 2026-10-16 14:05",
        "missing END label = job was cut: count back and report the last label number",
    ]


def test_blocks_are_start_labels_end_on_the_job_s_own_setup(site):
    m = site["reg"].media["big"]
    data = (site["root"] / "a.ezpl").read_bytes()
    picked = select_labels(data, "3-6", m)
    out = bk.with_bookends(m, picked, "a.ezpl", "3-6", 6, 4, "p1", TS)
    blocks = parse_blocks(out)
    assert len(blocks) == 4  # 2 label blocks + START + END
    assert out[len(blocks[0][0]) :].find(picked) > 0 and out.startswith(blocks[0][0])
    start, end = bk.bookend_blocks(m, picked, "a.ezpl", "3-6", 6, 4, "p1", TS)
    assert out == start + picked + end
    assert blocks[0][0] == parse_blocks(data)[0][0] == blocks[-1][0]  # the file's setup, not a fresh one
    # the marks pass the guards the labels pass, on every area of the web
    assert rz.blocks_outside_areas(out, m) == []
    assert rz.matching_media(out, site["reg"].media) == ["big"]
    assert rz.count_labels(out) == 4
    assert _ink(blocks[0], m) > 0.25 and _ink(blocks[-1], m) > 0.2 and _ink(blocks[1], m) == 0.0
    # START and END differ (the END carries the cut band), the two areas of one row carry the same mark
    s0 = bk.bookend_image(m, 0, "START", bk.start_lines("a.ezpl", "3-6", 6, 4, "p1", TS))
    e0 = bk.bookend_image(m, 0, "END", bk.end_lines("a.ezpl", 4, TS), cut=True)
    assert s0.size == (150, 100) and e0.size == (150, 100) and s0.tobytes() != e0.tobytes()
    assert (
        s0.tobytes()
        == bk.bookend_image(m, 1, "START", bk.start_lines("a.ezpl", "3-6", 6, 4, "p1", TS)).tobytes()
    )
    with pytest.raises(ValueError, match="too small"):
        bk.bookend_image(site["reg"].media["tiny"], 0, "START", ["x"])


def test_run_sends_n_plus_2_in_order_and_logs_the_flag(site):
    root, reg = site["root"], site["reg"]
    rows = plan(root, reg, [{"file": "a.ezpl", "labels": "3-6", "printer": "p1"}])
    assert rows[0].ok and rows[0].count == 4 and rows[0].total == 6
    txt = format_plan(rows, bookends=True)
    assert (
        "3-6 +2 bookends" in txt
        and "4 labels in 1 files → 4 on p1 (1 file, one stream) (+2 bookends per file)" in txt
    )
    assert "+2" not in format_plan(rows)
    log = root / LOG_NAME
    dry = run(root, reg, rows, log, dry_run=True, bookends=True)
    assert len(dry) == 2 and " → p1: 1 file, 4 blocks (2 bookends), " in dry[0]
    assert dry[1] == "dry run — +2 bookends per file, nothing sent"
    assert not site["t"][0].exists() and not log.exists()
    notes = run(root, reg, rows, log, bookends=True)
    assert notes[1].startswith("stream ") and " → p1: wrote " in notes[1]
    assert notes[2].startswith("a.ezpl labels 3-6 +2 bookends → sent in stream ")
    spool = site["t"][0].read_bytes()
    m = reg.media["big"]
    assert spool.startswith(b"^R10\r~Q-2\r")
    blocks = parse_blocks(spool)
    assert len(blocks) == 4
    picked = select_labels((root / "a.ezpl").read_bytes(), "3-6", m)
    assert spool[len(b"^R10\r~Q-2\r") + len(blocks[0][0]) :].find(picked) > 0
    assert _ink(blocks[0], m) > 0.25 and _ink(blocks[3], m) > 0.2 and _ink(blocks[1], m) == 0.0
    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["bookends"] is True and entry["count"] == 4 and entry["labels"] == "3-6"
    # the printer's heat retunes the START block = the head of the stream, as it retuned the first label
    rows2 = plan(root, reg, [{"file": "b.ezpl", "printer": "p2"}])
    run(root, reg, rows2, log, bookends=True)
    spool2 = site["t"][1].read_bytes()
    assert spool2.startswith(b"^Q30,2\r^W80\r~R200\r^AT\r^H12\r") and rz.count_labels(spool2) == 4
    assert json.loads(log.read_text().splitlines()[-1])["bookends"] is True


def test_without_the_flag_the_stream_is_byte_identical_to_0_12_1(site):
    """Regression pin: no flag = exactly offset + the selection, as every release before 0.13.0 sent it."""
    root, reg = site["root"], site["reg"]
    m = reg.media["big"]
    rows = plan(root, reg, [{"file": "a.ezpl", "labels": "1-3", "printer": "p1"}])
    log = root / LOG_NAME
    run(root, reg, rows, log)
    expected = offset_commands(m, (0.1, -0.02)) + select_labels((root / "a.ezpl").read_bytes(), "1-3", m)
    assert site["t"][0].read_bytes() == expected
    assert json.loads(log.read_text().splitlines()[-1])["bookends"] is False
    assert run(root, reg, rows, log, dry_run=True)[-1] == "dry run — nothing sent"
    site["t"][0].unlink()
    argv = [
        "--registry",
        str(site["regpath"]),
        "print",
        str(root / "a.ezpl"),
        "--printer",
        "p1",
        "--labels",
        "1-3",
    ]
    assert main(argv) == 0
    assert site["t"][0].read_bytes() == expected


def test_cli_print_bookends_dry_run_proof_and_send(site, capsys):
    root = site["root"]
    m = site["reg"].media["big"]
    pdf = root / "proof.pdf"
    argv = [
        "--registry",
        str(site["regpath"]),
        "print",
        str(root / "a.ezpl"),
        "--printer",
        "p1",
        "--bookends",
    ]
    assert main([*argv, "--labels", "3-6", "--dry-run", "--proof", str(pdf)]) == 0
    out = capsys.readouterr().out
    assert "would send 4 blocks (2 + 2 bookends per file) to p1" in out and "dry run — nothing sent" in out
    assert not site["t"][0].exists()
    raw = pdf.read_bytes()
    assert len(re.findall(rb"/Type\s*/Page\b(?!s)", raw)) == 4  # START, 2 label rows, END
    assert "proof: " in out and "(4 web rows" in out
    # the PNG sheet: START on top, END at the bottom
    sheet = root / "proof.png"
    assert main([*argv, "--labels", "3-6", "--dry-run", "--proof", str(sheet)]) == 0
    from PIL import Image

    with Image.open(sheet) as im:
        w, h = im.size
        row_h = h // 4
        top = im.crop((0, 0, w, row_h)).convert("L").histogram()[0]
        second = im.crop((0, row_h, w, 2 * row_h)).convert("L").histogram()[0]
        assert top > 4 * second and second < 2000  # the START row is mostly ink, a blank row only its scales
    # sent: N + 2 blocks in order, the labels unchanged in the middle
    assert main([*argv, "--labels", "3-6"]) == 0
    spool = site["t"][0].read_bytes()
    blocks = parse_blocks(spool)
    assert len(blocks) == 4 and spool.startswith(b"^R10\r~Q-2\r")
    picked = select_labels((root / "a.ezpl").read_bytes(), "3-6", m)
    assert picked in spool and _ink(blocks[0], m) > 0.25 and _ink(blocks[-1], m) > 0.2
    # the whole file and --first: the START names the range 1-N
    assert main([*argv, "--first", "1", "--dry-run"]) == 0
    assert "would send 3 blocks (1 + 2 bookends per file)" in capsys.readouterr().out
    # a media too small for the mark is a clean refusal, nothing sent
    with pytest.raises(SystemExit, match="too small"):
        main(
            [
                "--registry",
                str(site["regpath"]),
                "print",
                str(root / "t.ezpl"),
                "--printer",
                "small",
                "--bookends",
            ]
        )
    assert not site["t"][2].exists()


def test_cli_printcart_two_files_two_pairs(site, capsys):
    root = site["root"]
    argv = ["--registry", str(site["regpath"]), "printcart", "--root", str(root), "--bookends"]
    # the CLI picks the printer by media; two printers hold `big` → name them through a one-printer registry each
    reg1 = site["regpath"].with_name("one.yaml")
    reg1.write_text(REG.split("  p2:")[0].format(t1=site["t"][0], t2="", t3=""))
    argv = ["--registry", str(reg1), "printcart", "--root", str(root), "--bookends"]
    assert main([*argv, "--dry-run", "a.ezpl:1-4", "b.ezpl"]) == 0
    out = capsys.readouterr().out
    assert out.count("1-4 +2 bookends") == 2
    assert "8 labels in 2 files → 8 on p1 (2 files, one stream) (+2 bookends per file)" in out
    assert "2 files, 8 blocks (4 bookends)" in out
    assert "dry run — +2 bookends per file, nothing sent" in out and not site["t"][0].exists()
    # two files → ONE stream: START a END START b END, the log one line per file in that stream
    assert main([*argv, "a.ezpl:1-4", "b.ezpl"]) == 0
    m = site["reg"].media["big"]
    blocks = parse_blocks(site["t"][0].read_bytes())
    assert len(blocks) == 8 and [round(_ink(b, m) > 0.2) for b in blocks] == [1, 0, 0, 1, 1, 0, 0, 1]
    entries = [json.loads(ln) for ln in (root / LOG_NAME).read_text().splitlines()]
    assert [(e["file"], e["count"], e["bookends"], e["ok"]) for e in entries] == [
        ("a.ezpl", 4, True, True),
        ("b.ezpl", 4, True, True),
    ]
    assert entries[0]["stream"]["id"] == entries[1]["stream"]["id"]
    assert entries[1]["stream"]["offset"] == entries[0]["stream"]["offset"] + entries[0]["stream"]["length"]


def test_api_print_takes_bookends(site):
    """POST /api/print {"bookends": true}: the same semantics as the CLI, the flag echoed, the log marked."""
    import threading
    import urllib.request

    from rasterezpl.server import make_server

    srv = make_server(site["root"], "127.0.0.1", 0, str(site["regpath"]))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"

        def post(doc):
            req = urllib.request.Request(
                f"{base}/api/print",
                data=json.dumps(doc).encode(),
                headers={"content-type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())

        jobs = [{"file": "a.ezpl", "labels": "1-2", "printer": "p1"}]
        st, j = post({"jobs": jobs, "dry_run": True, "bookends": True})
        assert st == 200 and j["sent"] is False and j["bookends"] is True
        assert j["notes"][-1] == "dry run — +2 bookends per file, nothing sent" and not site["t"][0].exists()
        st, j = post({"jobs": jobs, "bookends": True})
        assert st == 200 and j["sent"] is True and j["bookends"] is True and j["plan"][0]["count"] == 2
        assert rz.count_labels(site["t"][0].read_bytes()) == 3  # one label row + START + END
        st, j = post({"jobs": jobs})
        assert j["bookends"] is False and rz.count_labels(site["t"][0].read_bytes()) == 1
        entries = [json.loads(ln) for ln in (site["root"] / LOG_NAME).read_text().splitlines()]
        assert [e["bookends"] for e in entries] == [True, False]
    finally:
        srv.shutdown()
        srv.server_close()


# --- 0.18.0: the mark covers the whole die-cut label when the media knows it -------------------------------
PAND = rz.PANDUIT_S150X225VATY_2UP  # label 450 × 675 dots, print-on block 450 × 225 at y 19, clear tail below


def test_full_label_mark_word_on_the_block_details_on_the_tail():
    assert bk.tag_rect(PAND, 0) == (38, 0, 450, 675) and bk.tag_rect(PAND, 1) == (488, 0, 450, 675)
    lines = bk.start_lines("labels-mtp-B29-6210-6122.ezpl", "2,4,6,8,10,14,16", 128, 7, "tdp43me", TS)
    img = bk.bookend_image(PAND, 0, "START", lines)
    assert img.size == (450, 675)
    hist = lambda im: im.histogram()[0] / (im.size[0] * im.size[1])  # noqa: E731 — black fraction
    block = img.crop((0, 19, 450, 244))  # the print-on area: a black field with the word
    tail = img.crop((0, 244, 450, 675))  # the clear tail: black text on white + a hazard band at the end
    assert hist(block) > 0.5
    assert 0.03 < hist(tail) < 0.3
    assert hist(img.crop((0, 0, 450, 19))) == 0.0  # the 0.063 in above the block stays clear
    # the tail's text is far larger than the block-only mark's small lines: its ink rows span most of the tail
    rows = [y for y in range(244, 640) if img.crop((20, y, 430, y + 1)).histogram()[0]]
    assert rows and rows[-1] - rows[0] > 150
    # the END mark: the cut band on the block, the legend on the tail
    end = bk.bookend_image(PAND, 0, "END", bk.end_lines("labels-mtp-B29-6210-6122.ezpl", 7, TS), cut=True)
    assert (
        end.size == (450, 675)
        and end.tobytes() != img.tobytes()
        and hist(end.crop((0, 244, 450, 675))) > 0.03
    )


def test_full_label_blocks_sit_on_the_label_rect_and_the_labels_stay_inside_the_areas(tmp_path):
    data = rz.job(PAND, [[None, None]] * 2)
    out = bk.with_bookends(PAND, data, "x.ezpl", "1-4", 4, 4, "tdp43me", TS)
    blocks = parse_blocks(out)
    assert len(blocks) == 4 and rz.matching_media(out, {"pand": PAND}) == ["pand"]
    # the bookend blocks' Q rows cover the whole label length (printer frame y 0 … 675), both labels across
    for b in (blocks[0], blocks[-1]):
        ys = [y for _x, y, _wb, _rows, _raw in b[1]]
        xs = {x for x, _y, _wb, _rows, _raw in b[1]}
        assert min(ys) < 40 and max(ys) > 600
        assert any(x < 100 for x in xs) and any(x > 450 for x in xs)
    # the label blocks are untouched and inside the print areas; only the two marks reach the tails
    assert rz.blocks_outside_areas(data, PAND) == []
    outside = {n for n, _x, _y in rz.blocks_outside_areas(out, PAND)}
    assert outside == {1, 4}
    # a media without a label outline keeps the block-only mark, byte for byte (the 0.13.0 look)
    plain = rz.Media(
        name="plain",
        dpi=PAND.dpi,
        width_mm=PAND.width_mm,
        length_mm=PAND.length_mm,
        gap_mm=PAND.gap_mm,
        areas_in=PAND.areas_in,
    )
    assert bk.tag_rect(plain, 0) == plain.area_px(0)
    assert bk.bookend_image(plain, 0, "START", ["a", "b"]).size == (450, 225)
    assert rz.blocks_outside_areas(bk.with_bookends(plain, data, "x.ezpl", "1-4", 4, 4, "p", TS), plain) == []


def test_balanced_wrap_breaks_between_fields_and_keeps_short_lines_whole():
    f = bk._font("Inter Bold", 30)
    w = int(f.getlength("7 labels · tdp43me · 2026-10-17 16:35") * 0.7)
    assert bk._balance(["7 labels · tdp43me · 2026-10-17 16:35"], f, w) == [
        "7 labels",
        "tdp43me",
        "2026-10-17 16:35",
    ]
    assert bk._balance(["short line"], f, w) == ["short line"]
    long = "missing END label = job was cut: count back and report the last label number"
    pieces = bk._balance([long], f, w)
    assert " ".join(pieces) == long and len(pieces) >= 2 and min(len(p.split()) for p in pieces) >= 3
