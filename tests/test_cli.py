"""The command line against a file target and a temporary registry — no printer."""

from __future__ import annotations

import pytest

import rasterezpl as rz
from rasterezpl.cli import main

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
    note: test
"""


def test_registry_text_print_decode(tmp_path, capsys):
    target = tmp_path / "spool.ezpl"
    reg = tmp_path / "printers.yaml"
    reg.write_text(REG.format(target=target))
    font = rz.find_font("Arial", "DejaVu Sans")
    if font is None:
        pytest.skip("no font")
    assert main(["--registry", str(reg), "printers"]) == 0
    assert "filep" in capsys.readouterr().out
    assert main(["--registry", str(reg), "media"]) == 0
    out = capsys.readouterr().out
    assert "tiny" in out and "panduit-s150x225vaty-2up" in out
    job = tmp_path / "job.ezpl"
    assert (
        main(
            [
                "--registry",
                str(reg),
                "text",
                "--media",
                "tiny",
                "--font",
                font,
                "--pt",
                "4",
                "a",
                "b",
                "c",
                "-o",
                str(job),
            ]
        )
        == 0
    )
    data = job.read_bytes()
    assert rz.count_labels(data) == 2  # 3 labels on a two-up media = 2 blocks
    # print via the registry printer: the offset goes in front, the media guard passes
    assert main(["--registry", str(reg), "print", str(job), "--printer", "filep", "--labels", "2"]) == 0
    spool = target.read_bytes()
    assert spool.startswith(b"^R10\r~Q-2\r") and rz.count_labels(spool) == 1
    # a job for another media is refused on that printer
    other = tmp_path / "other.ezpl"
    other.write_bytes(rz.job(rz.PANDUIT_S150X225VATY_2UP, [[None, None]]))
    with pytest.raises(SystemExit):
        main(["--registry", str(reg), "print", str(other), "--printer", "filep"])
    # decode to PNG
    png = tmp_path / "page.png"
    assert main(["--registry", str(reg), "decode", str(job), "-o", str(png)]) == 0
    assert (tmp_path / "page-1.png").exists() and (tmp_path / "page-2.png").exists()
    # ruler over the job to a file
    ruler = tmp_path / "ruler.ezpl"
    assert (
        main(
            ["--registry", str(reg), "ruler", str(job), "--to", str(ruler), "--media", "tiny", "--font", font]
        )
        == 0
    )
    assert rz.count_labels(ruler.read_bytes()) == 1
