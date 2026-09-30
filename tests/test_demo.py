"""The playground: the demo root is complete, its printers are file spools, and the whole pipeline runs on it."""

from __future__ import annotations

import pytest

import rasterezpl as rz
from rasterezpl.cli import main
from rasterezpl.demo import build
from rasterezpl.jobs import list_files, plan, run
from rasterezpl.registry import load

PIL = pytest.importorskip("PIL")


def test_demo_root_and_pipeline(tmp_path, capsys):
    info = build(tmp_path / "demo")
    root = info["root"]
    assert (root / "printers.yaml").exists() and (root / "README.txt").exists() and (root / "spool").is_dir()
    for rel in ("patterns/checker.ezpl", "patterns/stripes.ezpl", "patterns/gradient.ezpl"):
        assert rel in info["written"] and (root / rel).exists()
    reg = load(str(root / "printers.yaml"))
    assert set(reg.printers) == {"demo-panduit", "demo-godex"}
    assert reg.printers["demo-panduit"].transport.endswith("spool/demo-panduit.ezpl")
    # every demo file matches exactly one printer, so a cart plans without naming printers
    files = {f["file"]: f for f in list_files(root, reg)}
    assert (
        files["patterns/checker.ezpl"]["printers"] == ["demo-panduit"]
        and files["patterns/checker.ezpl"]["labels"] == 2
    )
    rows = plan(root, reg, [{"file": f} for f in info["written"]])
    assert all(r.ok for r in rows), [r.refused for r in rows]
    # patterns: the checkerboard has ink and both areas are used
    data = (root / "patterns" / "checker.ezpl").read_bytes()
    page = rz.decode_block(rz.parse_blocks(data)[0][1], rz.PANDUIT_S150X225VATY_2UP)
    assert page.getextrema() == (0, 255)
    # the whole pipeline: print to the spool, offset in front, then decode the spool
    notes = run(
        root, reg, plan(root, reg, [{"file": "patterns/stripes.ezpl", "labels": "1"}]), root / "log.jsonl"
    )
    spool = root / "spool" / "demo-panduit.ezpl"
    assert spool.exists() and "1 labels" in notes[0]
    assert (
        spool.read_bytes().startswith(b"^R3\r") and rz.count_labels(spool.read_bytes()) == 1
    )  # 0.01 in × 300 dpi
    png = root / "spool" / "out.png"
    assert main(["--registry", str(root / "printers.yaml"), "decode", str(spool), "-o", str(png)]) == 0
    assert png.exists()
    if rz.find_font("Arial", "DejaVu Sans") is None:
        assert info["skipped"] and "text/welcome.ezpl" not in info["written"]
    else:
        assert {"text/welcome.ezpl", "text/flags.ezpl"} <= set(info["written"])
        assert (
            files["text/flags.ezpl"]["printers"] == ["demo-godex"] and files["text/flags.ezpl"]["labels"] == 3
        )
        assert files["text/welcome.ezpl"]["labels"] == 4
    # the CLI: write the root only
    assert main(["demo", str(tmp_path / "demo2"), "--no-serve"]) == 0
    assert "demo root" in capsys.readouterr().out and (tmp_path / "demo2" / "printers.yaml").exists()
