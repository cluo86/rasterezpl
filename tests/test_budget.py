"""The byte budget on a printer record (0.24.0): a cart is split into streams of at most `max_stream_bytes`,
the first goes, the rest are HELD for the END label — and a cart under the budget is the one stream of 0.16.0,
byte for byte."""

from __future__ import annotations

import json

import pytest

import rasterezpl as rz
from rasterezpl import transport
from rasterezpl.cli import main
from rasterezpl.jobs import LOG_NAME, compose_streams, describe_stream, plan, run
from rasterezpl.media import offset_commands
from rasterezpl.registry import format_bytes, load, parse_bytes
from rasterezpl.stream import parse_blocks

REG = """
media:
  big:
    dpi: 100
    width_mm: 80
    length_mm: 30
    gap_mm: 2
    darkness: 10
    areas_in: [[0.1, 0.05, 1.5, 1.0], [1.6, 0.05, 1.5, 1.0]]
printers:
  p1:
    transport: "{t1}"
    media: big
    offset_in: [0.1, -0.02]
    darkness: 12
{budget}
"""
FILES = ("two.ezpl", "b30.ezpl", "b31.ezpl")


@pytest.fixture(autouse=True)
def fast_backoff(monkeypatch):
    monkeypatch.setattr(transport, "READY_BACKOFF_S", (0.05, 0.1, 0.2))


@pytest.fixture(autouse=True)
def frozen_minute(monkeypatch):
    from rasterezpl import bookends

    monkeypatch.setattr(bookends, "now", lambda: "2026-10-16 14:05")


def site(tmp_path, budget: str | None = None, transport: str | None = None):
    reg = tmp_path / "printers.yaml"
    t1 = tmp_path / "spool1.ezpl"
    line = f"    max_stream_bytes: {budget}" if budget is not None else ""
    reg.write_text(REG.format(t1=transport or t1.as_posix(), budget=line), encoding="utf-8")
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    m = r.media["big"]
    for name, n in zip(FILES, (2, 66, 66), strict=True):
        (root / name).write_bytes(rz.job(m, [[None, None]] * n))
    return {"reg": r, "regpath": reg, "root": root, "t1": t1}


def cart():
    return [{"file": f, "printer": "p1"} for f in FILES]


def test_parse_and_format_bytes():
    assert (
        parse_bytes(5_000_000)
        == parse_bytes("5 MB")
        == parse_bytes("5MB")
        == parse_bytes("5000000")
        == 5_000_000
    )
    assert (
        parse_bytes("4 MiB") == 4 * 1024**2
        and parse_bytes("512 KB") == 512_000
        and parse_bytes("1.5 GB") == 1_500_000_000
    )
    for bad in ("five", "5 XB", 0, -1, True, None, "0 MB"):
        with pytest.raises(ValueError):
            parse_bytes(bad)
    assert (
        format_bytes(5_000_000) == "5 MB"
        and format_bytes(4 * 1024**2) == "4 MiB"
        and format_bytes(512_000) == "512 KB"
    )
    assert format_bytes(7_229_016) == "7,229,016 bytes" and format_bytes(2_000_000_000) == "2 GB"


def test_the_registry_reads_the_budget(tmp_path):
    s = site(tmp_path, '"5 MB"')
    assert s["reg"].printers["p1"].max_stream_bytes == 5_000_000
    assert site(tmp_path)["reg"].printers["p1"].max_stream_bytes is None
    (tmp_path / "bad.yaml").write_text(
        REG.format(t1="x", budget="    max_stream_bytes: soon"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="printer p1: max_stream_bytes: not a byte size: 'soon'"):
        load(str(tmp_path / "bad.yaml"))


def test_a_cart_under_the_budget_is_the_one_stream_byte_for_byte(tmp_path):
    plain = site(tmp_path)
    rows = plan(plain["root"], plain["reg"], cart())
    [one] = compose_streams(plain["root"], plain["reg"], rows, bookends=True)
    assert one.total == 1 and one.budget is None and one.where == "→ p1"
    big = site(tmp_path, '"50 MB"')
    rows2 = plan(big["root"], big["reg"], cart())
    [same] = compose_streams(big["root"], big["reg"], rows2, bookends=True)
    assert same.data == one.data and same.total == 1 and same.budget == 50_000_000
    assert same.where == "→ p1 (budget 50 MB)" and [p.offset for p in same.parts] == [
        p.offset for p in one.parts
    ]
    assert describe_stream(one, True).startswith(f"stream {one.id} → p1: 3 files, 140 blocks (6 bookends), ")


def test_the_cart_is_split_at_the_budget_and_every_stream_is_a_job_of_its_own(tmp_path):
    plain = site(tmp_path)
    [one] = compose_streams(
        plain["root"], plain["reg"], plan(plain["root"], plain["reg"], cart()), bookends=True
    )
    p0, p1, p2 = one.parts
    budget = len(one.prefix) + p0.length + p1.length  # exactly two files fit; the third opens stream 2
    s = site(tmp_path, str(budget))
    rows = plan(s["root"], s["reg"], cart())
    streams = compose_streams(s["root"], s["reg"], rows, bookends=True)
    assert [(st.index, st.total, st.budget) for st in streams] == [(1, 2, budget), (2, 2, budget)]
    a, b = streams
    assert [p.row.file for p in a.parts] == ["two.ezpl", "b30.ezpl"] and [p.row.file for p in b.parts] == [
        "b31.ezpl"
    ]
    assert a.length == budget and b.length <= budget
    prefix = offset_commands(s["reg"].media["big"], (0.1, -0.02))
    # each stream: the registration prefix, its first part retuned to the printer's heat, offsets from its own start
    assert a.data == one.data[: a.length]  # the first stream IS the head of the one stream
    assert b.prefix == prefix and b.data.startswith(prefix + b"^Q30,2\r^W80\r~R200\r^AT\r^H12\r")
    assert parse_blocks(p2.data)[0][0].endswith(
        b"^H10\r^P1\r^L\r"
    )  # in the one stream b31 kept the file's heat
    assert b.parts[0].offset == len(prefix) and a.blocks + b.blocks == one.blocks == 140
    assert (
        a.where == f"→ p1 (1 of 2, budget {budget:,} bytes)"
        and b.where == f"→ p1 (2 of 2, budget {budget:,} bytes)"
    )
    assert describe_stream(b, True).startswith(
        f"stream {b.id} → p1 (2 of 2, budget {budget:,} bytes): 1 file, 68 blocks (2 bookends), "
    )
    # a budget one byte short of two files: three streams of one file each
    s3 = site(tmp_path, str(budget - 1))
    streams = compose_streams(s3["root"], s3["reg"], plan(s3["root"], s3["reg"], cart()), bookends=True)
    assert [[p.row.file for p in st.parts] for st in streams] == [["two.ezpl"], ["b30.ezpl"], ["b31.ezpl"]]
    assert [st.index for st in streams] == [1, 2, 3] and all(st.total == 3 for st in streams)


def test_a_file_over_the_budget_alone_is_refused_with_the_remedy(tmp_path):
    plain = site(tmp_path)
    [one] = compose_streams(
        plain["root"], plain["reg"], plan(plain["root"], plain["reg"], cart()), bookends=True
    )
    budget = len(one.prefix) + one.parts[1].length - 1  # b30 alone does not fit
    s = site(tmp_path, str(budget))
    with pytest.raises(
        ValueError,
        match=rf"b30.ezpl labels 1-132: [\d,]+ bytes alone, over p1's budget of {budget:,} bytes \(max_stream_bytes\) — give this file as two cart entries",
    ) as ei:
        compose_streams(s["root"], s["reg"], plan(s["root"], s["reg"], cart()), bookends=True)
    assert "fewer labels each" in str(ei.value)
    with pytest.raises(ValueError, match="over p1's budget"):
        run(s["root"], s["reg"], plan(s["root"], s["reg"], cart()), None, dry_run=True, bookends=True)


def test_run_holds_the_later_streams_for_the_end_label_and_all_streams_sends_them(tmp_path, fake_printer):
    plain = site(tmp_path)
    [one] = compose_streams(
        plain["root"], plain["reg"], plan(plain["root"], plain["reg"], cart()), bookends=True
    )
    budget = len(one.prefix) + one.parts[0].length + one.parts[1].length
    s = site(tmp_path, str(budget))
    root, reg = s["root"], s["reg"]
    fp = fake_printer()
    rows = plan(root, reg, cart())
    expect = compose_streams(root, reg, plan(root, reg, cart()), bookends=True)
    # the dry run describes every stream and its place
    notes = run(root, reg, rows, root / LOG_NAME, dry_run=True, bookends=True, to=fp.target)
    assert notes[0].startswith("stream ") and f"→ p1 (1 of 2, budget {budget:,} bytes): 2 files" in notes[0]
    assert f"→ p1 (2 of 2, budget {budget:,} bytes): 1 file" in notes[1] and notes[-1].endswith(
        "nothing sent"
    )
    # the real thing: stream 1 goes, stream 2 is held with the command to send it
    rows = plan(root, reg, cart())
    notes = run(root, reg, rows, root / LOG_NAME, bookends=True, to=fp.target)
    assert fp.wait_accepted(expect[0].length, 10.0) and fp.jobs() == [expect[0].data]
    assert [r.sent for r in rows] == [True, True, False]
    assert (
        rows[2].error
        == f"held: stream 2 of 2 for p1 (budget {budget:,} bytes) — the END label of stream 1 is the gate"
    )
    held_note = next(n for n in notes if ": HELD — " in n)
    assert (
        f"→ p1 (2 of 2, budget {budget:,} bytes): HELD — when the END label of stream 1 is in your hand: rasterezpl printcart --root {root} b31.ezpl:1-132 --bookends (or --all-streams"
        in held_note
    )
    assert "b31.ezpl labels 1-132 → HELD (stream 2 of 2)" in notes
    entries = [json.loads(ln) for ln in (root / LOG_NAME).read_text(encoding="utf-8").splitlines()]
    assert [e["ok"] for e in entries] == [True, True, False] and entries[2]["note"] == "held (stream 2 of 2)"
    assert entries[2]["error"].startswith("held:") and entries[2]["bytes_sent"] == 0
    # --all-streams: both go, in turn, each a connection of its own
    fp2 = fake_printer()
    rows = plan(root, reg, cart())
    notes = run(root, reg, rows, root / LOG_NAME, bookends=True, to=fp2.target, all_streams=True)
    assert fp2.wait_accepted(expect[0].length + expect[1].length, 10.0)
    assert fp2.jobs() == [expect[0].data, expect[1].data] and [r.sent for r in rows] == [True, True, True]
    assert sum(1 for n in notes if ": sent " in n) == 2 and not any("HELD" in n for n in notes)


def test_cli_exit_code_3_means_held(tmp_path, capsys, fake_printer):
    plain = site(tmp_path)
    [one] = compose_streams(
        plain["root"], plain["reg"], plan(plain["root"], plain["reg"], cart()), bookends=True
    )
    budget = len(one.prefix) + one.parts[0].length + one.parts[1].length
    fp = fake_printer()
    s = site(tmp_path, str(budget), transport=fp.target)  # p1 IS the fake printer
    args = ["--registry", str(s["regpath"]), "printcart", "--root", str(s["root"]), *FILES, "--bookends"]
    assert main(args) == 3
    out = capsys.readouterr().out
    assert "HELD" in out and "(1 of 2, budget" in out and "rasterezpl printcart --root" in out
    assert fp.wait_accepted(one.parts[0].length, 10.0)
    assert main([*args, "--all-streams"]) == 0
    assert "HELD" not in capsys.readouterr().out
