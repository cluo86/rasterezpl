"""The printer record's `settle` (0.15.0): the wait between cart files scales with the web rows just sent —
per_row_s × rows + floor_s — measured on the TDP43ME NetPS board (~1.3 s per row). File spools, no printer."""

from __future__ import annotations

import pytest

import rasterezpl as rz
from rasterezpl import transport
from rasterezpl.cli import main
from rasterezpl.jobs import LOG_NAME, format_plan, plan, run, set_waits
from rasterezpl.registry import load

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
  netps:
    transport: "{t1}"
    media: tiny
    settle:
      per_row_s: 1.5
      floor_s: 10
  plain:
    transport: "{t2}"
    media: tiny
"""


@pytest.fixture
def site(tmp_path, monkeypatch):
    reg = tmp_path / "printers.yaml"
    t1, t2 = tmp_path / "spool1.ezpl", tmp_path / "spool2.ezpl"
    reg.write_text(REG.format(t1=t1, t2=t2))
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir()
    (root / "one.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None]]))  # 1 web row
    (root / "big.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None]] * 66))  # 66 web rows
    slept: list[float] = []
    monkeypatch.setattr(transport.time, "sleep", lambda s: slept.append(s))
    return {"reg": r, "regpath": reg, "root": root, "t1": t1, "t2": t2, "slept": slept}


def test_record_keys_and_an_old_yaml(site, tmp_path):
    pr = site["reg"].printers["netps"]
    assert (pr.settle_per_row_s, pr.settle_floor_s) == (1.5, 10.0)
    assert pr.settle_for(1) == 12.0 and pr.settle_for(66) == 109.0 and pr.settle_for(68) == 112.0
    plain = site["reg"].printers["plain"]
    assert (plain.settle_per_row_s, plain.settle_floor_s) == (None, 0.0) and plain.settle_for(66) is None
    bad = tmp_path / "bad.yaml"
    bad.write_text(REG.format(t1="a", t2="b").replace("per_row_s: 1.5", "per_row: 1.5"))
    with pytest.raises(ValueError, match="settle is a block with per_row_s and floor_s"):
        load(str(bad))


def test_scaled_waits_in_plan_and_run(site):
    root, reg = site["root"], site["reg"]
    rows = plan(
        root, reg, [{"file": "one.ezpl", "printer": "netps"}, {"file": "big.ezpl", "printer": "netps"}]
    )
    assert [r.rows for r in rows] == [1, 66]
    set_waits(reg, rows)
    assert [r.wait_s for r in rows] == [12.0, 109.0]
    txt = format_plan(rows)
    assert "1-2 — then wait 12 s" in txt and "1-132 — then wait 109 s" in txt
    assert "between files: the printer record's settle, scaled to the rows just sent" in txt
    set_waits(reg, rows, bookends=True)
    assert [r.wait_s for r in rows] == [15.0, 112.0]  # two more rows each
    notes = run(root, reg, rows, root / LOG_NAME)  # (the tiny test media is too small for real bookends)
    assert site["slept"] == [
        pytest.approx(12.0, abs=0.5)
    ]  # the wait after file 1 only, from its send's return
    assert notes[1] == "settled 12 s before the next job (no status query)"
    # the override: every row the same, 0 = no wait at all (and no status query)
    set_waits(reg, rows, settle=30)
    assert [r.wait_s for r in rows] == [30.0, 30.0] and "then wait 30 s" in format_plan(rows, settle=30)
    site["slept"].clear()
    notes = run(root, reg, rows, root / LOG_NAME, settle=30)
    assert site["slept"] == [pytest.approx(30.0, abs=0.5)]
    set_waits(reg, rows, settle=0)
    assert [r.wait_s for r in rows] == [0.0, 0.0] and "then no wait" in format_plan(rows, settle=0)
    assert "between files: no wait (--settle 0)" in format_plan(rows, settle=0)
    site["slept"].clear()
    notes = run(root, reg, rows, root / LOG_NAME, settle=0)
    assert site["slept"] == [] and notes[1] == "no wait before the next job (settle 0)"


def test_a_printer_without_settle_polls_as_in_0_14_0(site):
    root, reg = site["root"], site["reg"]
    rows = plan(
        root, reg, [{"file": "one.ezpl", "printer": "plain"}, {"file": "big.ezpl", "printer": "plain"}]
    )
    set_waits(reg, rows)
    assert [r.wait_s for r in rows] == [None, None] and "then wait" not in format_plan(rows)
    assert "between files: poll the printer's status until it answers 00" in format_plan(rows)
    notes = run(root, reg, rows, root / LOG_NAME)
    assert site["slept"] == [] and notes[1] == "printer ready (00 file spool (writable))"


def test_cli_printcart_states_the_computed_wait(site, capsys):
    reg1 = site["regpath"].with_name("one.yaml")
    reg1.write_text(REG.split("  plain:")[0].format(t1=site["t1"], t2=""))
    argv = ["--registry", str(reg1), "printcart", "--root", str(site["root"]), "one.ezpl", "big.ezpl"]
    assert main([*argv, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "then wait 12 s" in out and "then wait 109 s" in out and site["slept"] == []
    assert main([*argv, "--settle", "30", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert out.count("then wait 30 s") == 2 and "settle 30 s, no status query" in out
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert site["slept"] == [pytest.approx(12.0, abs=0.5)] and "settled 12 s before the next job" in out
