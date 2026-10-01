"""The clear-laminate layouts on the full-label media variant, and the same-stock rule that lets the standard
printer take such a job."""

from __future__ import annotations

import pytest

import rasterezpl as rz
from rasterezpl.compose import Spec, render
from rasterezpl.images import placeholder_logo
from rasterezpl.jobs import holds, list_files, plan
from rasterezpl.layouts import WHITE_FRAC, render_label
from rasterezpl.registry import load
from rasterezpl.text import bundled_fonts

PIL = pytest.importorskip("PIL")


def test_full_media_is_the_same_stock_with_the_whole_label_allowed():
    std, full, m203 = (
        rz.PANDUIT_S150X225VATY_2UP,
        rz.PANDUIT_S150X225VATY_2UP_FULL,
        rz.PANDUIT_S150X225VATY_2UP_203,
    )
    assert full.same_stock(std) and std.same_stock(full) and not std.same_stock(m203)
    assert full.areas_in == std.labels_in and full.area_px(0)[3] > std.area_px(0)[3]


def test_clear_layouts_put_text_on_the_white_third_and_the_picture_below():
    full = rz.PANDUIT_S150X225VATY_2UP_FULL
    _, _, w, h = full.area_px(0)
    inter = bundled_fonts()["Inter Bold"]
    white_h = int(h * WHITE_FRAC)
    im = render_label(w, h, ["Jane Doe", "C3000071A605"], inter, 20, "clear-qr", qr="C3000071A605")
    assert im.size == (w, h)
    assert im.crop((0, 0, w, white_h)).getextrema() == (0, 255)  # text on the white third
    clear = im.crop((0, white_h, w, h))
    assert clear.getextrema() == (0, 255) and clear.getbbox() is not None
    # the code is large: its ink spans more than half the clear part's height
    from PIL import ImageOps

    bbox = ImageOps.invert(clear).getbbox()
    assert bbox and (bbox[3] - bbox[1]) > (h - white_h) * 0.5
    im2 = render_label(w, h, ["ASSET 1"], inter, 20, "clear-image", image=placeholder_logo())
    assert im2.crop((0, white_h, w, h)).getextrema() == (0, 255)
    with pytest.raises(ValueError, match="needs QR"):
        render_label(w, h, ["x"], inter, 20, "clear-qr")
    with pytest.raises(ValueError, match="needs an image"):
        render_label(w, h, ["x"], inter, 20, "clear-image")


def test_full_label_job_plans_onto_the_standard_printer(tmp_path):
    reg = load(None)
    spec = Spec.from_dict(
        {
            "media": "panduit-s150x225vaty-2up-full",
            "font": "Inter Bold",
            "pt": 6,
            "layout": "clear-qr",
            "text": "Jane Doe\nC3000071A605",
            "qr": "C3000071A605",
        }
    )
    data, m, _, _ = render(reg, spec)
    assert m is rz.PANDUIT_S150X225VATY_2UP_FULL and rz.count_labels(data) == 1
    # blocks lie on the clear part: outside the STANDARD media's areas, inside the full variant's
    assert rz.blocks_outside_areas(data, rz.PANDUIT_S150X225VATY_2UP) and not rz.blocks_outside_areas(data, m)
    regf = tmp_path / "r.yaml"
    regf.write_text(
        f'printers:\n  p300:\n    transport: "{tmp_path / "s300.ezpl"}"\n    media: panduit-s150x225vaty-2up\n'
        f'  p203:\n    transport: "{tmp_path / "s203.ezpl"}"\n    media: panduit-s150x225vaty-2up-203\n'
    )
    r = load(str(regf))
    root = tmp_path / "root"
    root.mkdir()
    (root / "clear.ezpl").write_bytes(data)
    assert holds(r.printers["p300"], ["panduit-s150x225vaty-2up-full"], r)
    assert not holds(r.printers["p203"], ["panduit-s150x225vaty-2up-full"], r)
    listed = list_files(root, r)[0]
    assert listed["media"] == ["panduit-s150x225vaty-2up-full"] and listed["printers"] == ["p300"]
    rows = plan(root, r, [{"file": "clear.ezpl"}])
    assert rows[0].ok and rows[0].printer == "p300" and rows[0].media == "panduit-s150x225vaty-2up-full"
    rows = plan(root, r, [{"file": "clear.ezpl", "printer": "p203"}])
    assert not rows[0].ok and "p203 holds" in rows[0].refused
