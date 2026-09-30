"""The print-job layer on its own, and `rasterezpl printcart` over it — file targets, no printer."""

from __future__ import annotations

import json

import pytest

import rasterezpl as rz
from rasterezpl.cli import main
from rasterezpl.jobs import LOG_NAME, format_plan, parse_cart, plan, run
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
  filep:
    transport: "{target}"
    media: tiny
    offset_in: [0.1, -0.02]
  filep2:
    transport: "{target2}"
    media: tiny
"""


def test_parse_cart():
    assert parse_cart(["a/b.ezpl:1-4,7", "c.ezpl", "d.ezpl:"]) == [
        {"file": "a/b.ezpl", "labels": "1-4,7"},
        {"file": "c.ezpl", "labels": ""},
        {"file": "d.ezpl", "labels": ""},
    ]
    with pytest.raises(ValueError):
        parse_cart(["a.ezpl:x-"])


@pytest.fixture
def site(tmp_path):
    reg = tmp_path / "printers.yaml"
    t1, t2 = tmp_path / "spool1.ezpl", tmp_path / "spool2.ezpl"
    reg.write_text(REG.format(target=t1, target2=t2))
    r = load(str(reg))
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.ezpl").write_bytes(rz.job(r.media["tiny"], [[None, None], [None, None]]))  # 4 labels
    return {"reg": r, "regpath": reg, "root": root, "t1": t1, "t2": t2}


def test_plan_ambiguity_format_and_run(site):
    root, reg = site["root"], site["reg"]
    rows = plan(root, reg, [{"file": "a.ezpl", "labels": "1-3"}])
    assert not rows[0].ok and "choose a printer: filep, filep2" in (rows[0].refused or "")
    rows = plan(
        root,
        reg,
        [{"file": "a.ezpl", "labels": "1-3", "printer": "filep"}, {"file": "a.ezpl", "printer": "filep2"}],
    )
    assert [r.ok for r in rows] == [True, True] and rows[1].labels == "1-4" and rows[1].count == 4
    txt = format_plan(rows)
    assert "7 labels in 2 files → 3 on filep, 4 on filep2" in txt
    # dry run, then the real thing, logged
    log = root / LOG_NAME
    assert run(root, reg, rows, log, dry_run=True) == ["dry run — nothing sent"] and not log.exists()
    notes = run(root, reg, rows, log)
    assert len(notes) == 2 and site["t1"].exists() and site["t2"].exists()
    assert rz.count_labels(site["t1"].read_bytes()) == 2 and site["t1"].read_bytes().startswith(
        b"^R10\r~Q-2\r"
    )
    entries = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [(e["printer"], e["count"]) for e in entries] == [("filep", 3), ("filep2", 4)]
    # a refused row stops everything
    site["t1"].unlink()
    bad = plan(
        root,
        reg,
        [{"file": "a.ezpl", "labels": "9", "printer": "filep"}, {"file": "a.ezpl", "printer": "filep2"}],
    )
    assert run(root, reg, bad, log)[0].startswith("REFUSED") and not site["t1"].exists()


def test_cli_printcart(site, capsys):
    argv = ["--registry", str(site["regpath"]), "printcart", "--root", str(site["root"])]
    # ambiguous printer → refused plan, exit 2, nothing written
    assert main([*argv, "a.ezpl:1-2"]) == 2
    assert "choose a printer" in capsys.readouterr().out and not site["t1"].exists()
    # the plan alone
    assert main([*argv, "--dry-run", "a.ezpl:1-2"]) == 2  # still refused: ambiguous
    # the CLI takes FILE:LABELS only, the printer comes from the file's media — a one-printer registry
    reg1 = site["regpath"].with_name("one.yaml")
    reg1.write_text(REG.split("  filep2:")[0].format(target=site["t1"], target2=""))
    argv1 = ["--registry", str(reg1), "printcart", "--root", str(site["root"])]
    assert main([*argv1, "--dry-run", "a.ezpl:1-2"]) == 0
    out = capsys.readouterr().out
    assert "2 labels in 1 files → 2 on filep" in out and "dry run" in out and not site["t1"].exists()
    assert main([*argv1, "a.ezpl:1-2"]) == 0
    assert rz.count_labels(site["t1"].read_bytes()) == 1
    assert (site["root"] / LOG_NAME).exists()
