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
    # `to` overrides the transport for every row (a test spool), offset and media still the printer's
    alt = root.parent / "alt.ezpl"
    run(root, reg, rows[:1], log, to=str(alt))
    assert alt.read_bytes().startswith(b"^R10\r~Q-2\r") and json.loads(log.read_text().splitlines()[-1])[
        "transport"
    ] == str(alt)
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
    # a job file given as --root is the common slip: refuse with the command that was meant
    with pytest.raises(SystemExit) as e:
        main(
            [
                "--registry",
                str(site["regpath"]),
                "printcart",
                "--root",
                str(site["root"] / "a.ezpl"),
                "1",
                "--dry-run",
            ]
        )
    assert "did you mean" in str(e.value) and "--dry-run" in str(e.value)
    with pytest.raises(SystemExit):
        main(
            ["--registry", str(site["regpath"]), "printcart", "--root", str(site["root"] / "nope"), "a.ezpl"]
        )
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


def test_same_stock_other_dpi_is_not_the_same_media(tmp_path):
    """The Panduit stock exists as a 300 dpi and a 203 dpi media with the SAME mm header: a job composed for the
    203 variant must not be planned onto the 300 dpi printer (its blocks land elsewhere) — the user's 2026-09-30
    demo case — and is planned onto a 203 printer when one exists."""
    from rasterezpl.jobs import list_files, written_for
    from rasterezpl.presets import PANDUIT_S150X225VATY_2UP_203

    reg300 = tmp_path / "r300.yaml"
    reg300.write_text(
        f'printers:\n  p300:\n    transport: "{tmp_path / "s300.ezpl"}"\n    media: panduit-s150x225vaty-2up\n'
    )
    root = tmp_path / "root"
    root.mkdir()
    from PIL import Image

    m203 = PANDUIT_S150X225VATY_2UP_203
    _, _, aw, ah = m203.area_px(0)
    ink = Image.new("L", (aw, ah), 0)  # a job with ink: its blocks sit where the 203 dpi areas are
    (root / "j203.ezpl").write_bytes(rz.job(m203, [[ink, ink]]))
    r = load(str(reg300))
    fits, header_only = written_for((root / "j203.ezpl").read_bytes(), r)
    assert fits == ["panduit-s150x225vaty-2up-203"] and header_only == ["panduit-s150x225vaty-2up"]
    rows = plan(root, r, [{"file": "j203.ezpl"}])
    assert (
        not rows[0].ok
        and "p300 holds panduit-s150x225vaty-2up" in rows[0].refused
        and "another dpi" in rows[0].refused
    )
    rows = plan(root, r, [{"file": "j203.ezpl", "printer": "p300"}])
    assert not rows[0].ok and "same stock at another dpi" in rows[0].refused
    listed = list_files(root, r)[0]  # the file fits the 203 preset, which no printer here holds
    assert listed["printers"] == [] and listed["media"] == ["panduit-s150x225vaty-2up-203"]
    reg_both = tmp_path / "rboth.yaml"
    reg_both.write_text(
        reg300.read_text()
        + f'  p203:\n    transport: "{tmp_path / "s203.ezpl"}"\n    media: panduit-s150x225vaty-2up-203\n'
    )
    rows = plan(root, load(str(reg_both)), [{"file": "j203.ezpl"}])
    assert rows[0].ok and rows[0].printer == "p203"
