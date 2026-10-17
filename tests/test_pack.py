"""0.17.0: a partial selection on a multi-across web is PACKED into full rows — the kept labels in label order,
area by area, no blank except possibly on the last row. Whole-row selections are byte-identical to before;
--no-pack keeps today's blank-the-rest rows; a media whose areas differ in size is never packed. File spools only."""

from __future__ import annotations

import json

import pytest

import rasterezpl as rz
from rasterezpl.cli import main
from rasterezpl.jobs import (
    LOG_NAME,
    Part,
    PlanRow,
    compose_streams,
    cut_point,
    describe_stream,
    format_plan,
    plan,
    run,
)
from rasterezpl.registry import load
from rasterezpl.stream import (
    decode_block,
    pack_blocker,
    pack_labels,
    parse_blocks,
    select_blocks,
    select_labels,
)

PIL = pytest.importorskip("PIL")

REG = """
media:
  big:
    dpi: 100
    width_mm: 80
    length_mm: 30
    gap_mm: 2
    areas_in: [[0.1, 0.05, 1.5, 1.0], [1.6, 0.05, 1.5, 1.0]]
  lop:
    dpi: 100
    width_mm: 80
    length_mm: 30
    gap_mm: 3
    areas_in: [[0.1, 0.05, 1.5, 1.0], [1.7, 0.05, 1.2, 1.0]]
printers:
  p1:
    transport: "{t1}"
    media: big
    offset_in: [0.1, -0.02]
  p2:
    transport: "{t2}"
    media: lop
"""

SEVEN = (
    "2,4,6,8,10,14,16"  # one side of seven cables on a 2-across web (labels-mtp-B29-6210-6122, 2026-10-17)
)


def _label(m, k: int):
    """Label k's image: a black bar whose position is the label's number — every crop is recognisable."""
    from PIL import Image, ImageDraw

    _, _, aw, ah = m.area_px(0)
    img = Image.new("L", (aw, ah), 255)
    ImageDraw.Draw(img).rectangle((k, 10, k + 3, ah - 10), fill=0)
    return img


def _site(tmp_path, blocks=64):
    reg = tmp_path / "printers.yaml"
    t1, t2 = tmp_path / "spool1.ezpl", tmp_path / "spool2.ezpl"
    reg.write_text(REG.format(t1=t1, t2=t2))
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir()
    for name, m in (("job.ezpl", r.media["big"]), ("lop.ezpl", r.media["lop"])):
        if name == "lop.ezpl":
            labels = [[_label(m, 2 * b + 1), None] for b in range(blocks)]  # the narrow area 1 stays blank
        else:
            labels = [[_label(m, 2 * b + 1), _label(m, 2 * b + 2)] for b in range(blocks)]
        (root / name).write_bytes(rz.job(m, labels))
    return {"reg": r, "regpath": reg, "root": root, "t1": t1, "t2": t2}


def _crops(data: bytes, m):
    """Every block of a stream as its areas' crops (printer frame bytes), block by block."""
    return [
        [decode_block(qs, m).crop(_rect(m, a)).tobytes() for a in range(m.labels_per_block)]
        for _setup, qs in parse_blocks(data)
    ]


def _rect(m, a):
    x, y, w, h = m.printer_rect(a)
    return (x, y, x + w, y + h)


def test_seven_one_side_labels_pack_into_four_rows(tmp_path):
    site = _site(tmp_path)
    m, data = site["reg"].media["big"], (site["root"] / "job.ezpl").read_bytes()
    orig = _crops(data, m)
    assert len(orig) == 64 and len({c for row in orig for c in row}) == 128  # every label distinct
    blank = orig[0][0].__class__(b"\xff" * len(orig[0][0]))
    packed, is_packed, note = pack_labels(data, SEVEN, m)
    assert is_packed is True and note is None
    rows = _crops(packed, m)
    assert len(rows) == 4
    label = lambda k: orig[(k - 1) // 2][(k - 1) % 2]  # noqa: E731
    assert rows[0] == [label(2), label(4)]
    assert rows[1] == [label(6), label(8)]
    assert rows[2] == [label(10), label(14)]
    assert rows[3] == [label(16), blank]
    # the packed stream is encoded on the file's own setup, at the media's rects, nothing outside the areas
    assert rz.blocks_outside_areas(packed, m) == []
    assert parse_blocks(packed)[0][0] == parse_blocks(data)[0][0]
    # today's behaviour, for comparison: 7 rows, the left area of each blank
    loose = _crops(select_labels(data, SEVEN, m), m)
    assert (
        len(loose) == 7
        and all(r[0] == blank for r in loose)
        and [r[1] for r in loose] == [label(k) for k in (2, 4, 6, 8, 10, 14, 16)]
    )


def test_whole_rows_are_byte_identical_and_never_marked_packed(tmp_path):
    site = _site(tmp_path)
    m, data = site["reg"].media["big"], (site["root"] / "job.ezpl").read_bytes()
    for spec in ("1-128", "3-4", "1,2,127,128", "5-8"):
        out, is_packed, note = pack_labels(data, spec, m)
        assert (is_packed, note) == (False, None), spec
        assert out == select_labels(data, spec, m), spec
    assert pack_labels(data, "1-128", m)[0] == data
    assert pack_labels(data, "3-4", m)[0] == select_blocks(data, [2])


def test_mixed_selection_keeps_the_full_row_then_packs_the_rest(tmp_path):
    site = _site(tmp_path)
    m, data = site["reg"].media["big"], (site["root"] / "job.ezpl").read_bytes()
    orig = _crops(data, m)
    out, is_packed, _ = pack_labels(data, "1,2,4", m)
    assert is_packed is True
    rows = _crops(out, m)
    blank = b"\xff" * len(orig[0][0])
    assert rows == [[orig[0][0], orig[0][1]], [orig[1][1], blank]]
    # the full row [1,2] is block 1 byte for byte
    assert out.startswith(select_blocks(data, [1]))
    # 2,3 — a label from each of two rows — share one row
    rows = _crops(pack_labels(data, "2,3", m)[0], m)
    assert rows == [[orig[0][1], orig[1][0]]]


def test_unequal_areas_are_not_packed_and_say_so(tmp_path):
    site = _site(tmp_path)
    reg, root = site["reg"], site["root"]
    m = reg.media["lop"]
    assert pack_blocker(reg.media["big"]) is None
    assert pack_blocker(m) == "print areas differ in size (area 0 150×100, area 1 120×100)"
    data = (root / "lop.ezpl").read_bytes()
    out, is_packed, note = pack_labels(data, "1,3,5", m)
    assert (
        is_packed is False
        and note == "not packed: print areas differ in size (area 0 150×100, area 1 120×100)"
    )
    assert out == select_labels(data, "1,3,5", m) and rz.count_labels(out) == 3
    rows = plan(root, reg, [{"file": "lop.ezpl", "labels": "1,3,5"}])
    r = rows[0]
    assert (r.ok, r.rows, r.packed, r.pack_note) == (True, 3, False, note)
    assert (
        "lop.ezpl       3  p2         1,3,5 (3 rows, not packed: print areas differ in size"
        in format_plan(rows)
    )
    st = compose_streams(root, reg, rows)[0]
    assert st.parts[
        0
    ].packed is False and "(3 rows, not packed: print areas differ in size" in describe_stream(st, False)


def test_plan_stream_log_and_cli_report_the_packed_rows(tmp_path, capsys):
    site = _site(tmp_path)
    reg, root = site["reg"], site["root"]
    rows = plan(root, reg, [{"file": "job.ezpl", "labels": SEVEN}])
    r = rows[0]
    assert (r.count, r.total, r.rows, r.packed, r.pack_note) == (7, 128, 4, True, None)
    txt = format_plan(rows, bookends=True)
    assert f"job.ezpl       7  p1         {SEVEN} → 4 rows (packed) +2 bookends" in txt
    assert "7 labels in 1 files → 7 on p1 (1 file, one stream) (+2 bookends per file)" in txt
    streams = compose_streams(root, reg, rows, bookends=True)
    st = streams[0]
    assert st.blocks == 6 and st.parts[0].rows == 6 and st.parts[0].packed is True
    assert f"job.ezpl labels {SEVEN} (7 labels → 4 rows, packed)" in describe_stream(st, True)
    # the --no-pack plan: 7 rows as today
    loose = plan(root, reg, [{"file": "job.ezpl", "labels": SEVEN}], pack=False)
    assert (loose[0].rows, loose[0].packed) == (7, False) and "(packed)" not in format_plan(loose)
    assert compose_streams(root, reg, loose, pack=False)[0].blocks == 7
    # sent to the spool: 4 label rows + 2 bookends, the log line carries rows + packed + the selection as given
    notes = run(root, reg, rows, root / LOG_NAME, bookends=True)
    spool = site["t1"].read_bytes()
    assert rz.count_labels(spool) == 6
    body = spool[len(st.prefix) :]
    labels_only = parse_blocks(body)[1:-1]
    m = reg.media["big"]
    assert [[decode_block(qs, m).crop(_rect(m, a)).tobytes() for a in (0, 1)] for _s, qs in labels_only][3][
        1
    ] == b"\xff" * (150 * 100)
    e = json.loads((root / LOG_NAME).read_text().splitlines()[-1])
    assert (e["labels"], e["count"], e["rows"], e["packed"], e["bookends"], e["ok"]) == (
        SEVEN,
        7,
        4,
        True,
        True,
        True,
    )
    assert notes[-1].startswith(f"job.ezpl labels {SEVEN} +2 bookends → sent in stream ")
    # the CLI: printcart dry run names the packing; --no-pack does not pack
    site["t1"].unlink()
    argv = ["--registry", str(site["regpath"]), "printcart", "--root", str(root), f"job.ezpl:{SEVEN}"]
    assert main([*argv, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert (
        f"{SEVEN} → 4 rows (packed)" in out
        and "(7 labels → 4 rows, packed)" in out
        and not site["t1"].exists()
    )
    assert main([*argv, "--no-pack"]) == 0
    out = capsys.readouterr().out
    assert "(packed)" not in out and "(7 rows)" in out and rz.count_labels(site["t1"].read_bytes()) == 7
    # print: the same selection, the same packing, --no-pack the same 7 rows
    site["t1"].unlink()
    pargs = [
        "--registry",
        str(site["regpath"]),
        "print",
        str(root / "job.ezpl"),
        "--printer",
        "p1",
        "--labels",
        SEVEN,
    ]
    assert main([*pargs, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would send 4 blocks to p1 — 7 labels → 4 rows (packed)" in out and not site["t1"].exists()
    assert main(pargs) == 0
    assert rz.count_labels(site["t1"].read_bytes()) == 4
    e = json.loads((root / LOG_NAME).read_text().splitlines()[-1])
    assert (e["labels"], e["count"], e["rows"], e["packed"]) == (SEVEN, 7, 4, True)
    site["t1"].unlink()
    assert main([*pargs, "--no-pack"]) == 0
    assert rz.count_labels(site["t1"].read_bytes()) == 7
    e = json.loads((root / LOG_NAME).read_text().splitlines()[-1])
    assert (e["rows"], e["packed"]) == (7, False)


def test_a_cut_inside_a_packed_stream_resumes_from_the_rows_first_label(tmp_path):
    site = _site(tmp_path)
    reg, root = site["reg"], site["root"]
    rows = plan(root, reg, [{"file": "job.ezpl", "labels": SEVEN}])
    part = compose_streams(root, reg, rows)[0].parts[0]
    blocks = parse_blocks(part.data)
    # packed row 3 holds labels 10 and 14: a cut inside it resumes at 10, to the selection's end
    lengths = [len(rz.serialise_block(s, q)) for s, q in blocks]
    inside_row3 = part.offset + sum(lengths[:2]) + lengths[2] // 2
    assert cut_point(part, inside_row3, False) == ("cut inside job.ezpl at web row 3 of 4", "10-16")
    inside_row4 = part.offset + sum(lengths[:3]) + 5
    assert cut_point(part, inside_row4, False) == ("cut inside job.ezpl at web row 4 of 4", "16")
    # the unpacked part (today's rows) still resumes from the original row's first selected label
    loose = Part(
        PlanRow("job.ezpl", SEVEN, 7, 128, "p1", "big", None),
        select_labels((root / "job.ezpl").read_bytes(), SEVEN, reg.media["big"]),
        0,
        7,
        2,
    )
    ll = [len(rz.serialise_block(s, q)) for s, q in parse_blocks(loose.data)]
    assert cut_point(loose, sum(ll[:2]) + 3, False) == ("cut inside job.ezpl at web row 3 of 7", "6-16")
