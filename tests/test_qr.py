"""QR codes on labels: the encoder image, the badge layouts, and the composer's per-label QR data."""

from __future__ import annotations

import pytest
import segno

import rasterezpl as rz
from rasterezpl.compose import Spec, render
from rasterezpl.images import qr_image
from rasterezpl.layouts import render_label
from rasterezpl.registry import load
from rasterezpl.text import bundled_fonts

PIL = pytest.importorskip("PIL")


def test_qr_image_is_the_symbol_at_whole_module_scale():
    q = segno.make("C3000071A605", error="m", micro=False)
    n = q.symbol_size(scale=1, border=0)[0]
    im = qr_image("C3000071A605", 200, border=2)
    scale = 200 // (n + 4)
    assert im.size == ((n + 4) * scale, (n + 4) * scale) and im.getextrema() == (0, 255)
    # the top-left finder pattern: dark at its outer ring, light just inside it
    b = 2 * scale
    assert im.getpixel((b + scale // 2, b + scale // 2)) == 0
    assert im.getpixel((b + scale + scale // 2, b + scale + scale // 2)) == 255
    # the quiet zone is white
    assert im.getpixel((scale // 2, scale // 2)) == 255
    with pytest.raises(ValueError):
        qr_image("", 200)
    with pytest.raises(ValueError):
        qr_image("x" * 200, 20)  # too many modules for the box


def test_badge_layouts_and_composer():
    m = rz.PANDUIT_S150X225VATY_2UP
    _, _, w, h = m.area_px(0)
    inter = bundled_fonts()["Inter Bold"]
    for lay in ("badge", "qr-left", "qr"):
        im = render_label(w, h, ["Jane Doe", "C3000071A605"], inter, 18, lay, qr="C3000071A605")
        assert im.size == (w, h) and im.getextrema() == (0, 255)
        with pytest.raises(ValueError, match="needs QR"):
            render_label(w, h, ["x"], inter, 18, lay)
    # badge: the code sits in the upper part, the text below it
    im = render_label(w, h, ["Jane Doe"], inter, 18, "badge", qr="C3000071A605")
    assert im.crop((0, 0, w, h // 2)).getextrema() == (0, 255)
    reg = load(None)
    rows = "name\tid\torg\nJane Doe\tC3000071A605\tAcme\nJohn Roe\tC3000071A606\tAcme"
    spec = Spec.from_dict(
        {
            "media": "panduit-s150x225vaty-2up",
            "font": "Inter Bold",
            "pt": 6,
            "layout": "badge",
            "rows": rows,
            "template": "{name}\\n{id}\\n{org}",
            "qr": "{id}",
        }
    )
    assert spec.qrs == ["C3000071A605", "C3000071A606"]
    data, _, _, texts = render(reg, spec)
    assert (
        texts == ["Jane Doe\nC3000071A605\nAcme", "John Roe\nC3000071A606\nAcme"]
        and rz.count_labels(data) == 1
    )
    # text input with {n} in the code, copies expand both
    spec2 = Spec.from_dict(
        {
            "media": "panduit-s150x225vaty-2up",
            "font": "Inter",
            "pt": 6,
            "layout": "qr-left",
            "text": "tag {n}\n\ntag {n}",
            "qr": "ASSET-{n}",
            "copies": 2,
            "start": 7,
        }
    )
    assert spec2.qrs == ["ASSET-{n}", "ASSET-{n}"]
    data, _, _, texts = render(reg, spec2)
    assert texts == ["tag 7", "tag 7", "tag 8", "tag 8"] and rz.count_labels(data) == 2
    # a QR alone, no text at all
    data, _, _, texts = render(
        reg,
        Spec.from_dict(
            {
                "media": "panduit-s150x225vaty-2up",
                "font": "Inter",
                "pt": 6,
                "layout": "qr",
                "qr": "https://example.com/asset/1",
            }
        ),
    )
    assert texts == [""] and rz.count_labels(data) == 1
    with pytest.raises(ValueError, match="needs QR"):
        render(
            reg,
            Spec.from_dict(
                {
                    "media": "panduit-s150x225vaty-2up",
                    "font": "Inter",
                    "pt": 6,
                    "layout": "badge",
                    "text": "a",
                }
            ),
        )
