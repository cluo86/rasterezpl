"""Code 128 on labels: the encoder (values, checksum, modules), the image, the barcode layouts, the composer —
and a round trip through a real decoder (zxing-cpp) when it is installed."""

from __future__ import annotations

import pytest

import rasterezpl as rz
from rasterezpl.compose import Spec, render
from rasterezpl.images import code128_image, code128_modules, code128_values
from rasterezpl.layouts import render_label
from rasterezpl.registry import load
from rasterezpl.text import bundled_fonts

PIL = pytest.importorskip("PIL")

RACK = "IAD108.1 SC77 R6225/A"  # a rack label read off a data-hall rail (Code 128, the text verbatim)


def test_values_checksum_and_sets():
    v = code128_values("PJJ123C")  # start B; "123" is under four digits so it stays in set B
    assert v[0] == 104 and v[-1] == 106
    assert v[1:-2] == [48, 42, 42, 17, 18, 19, 35]
    assert v[-2] == (104 + 48 + 2 * 42 + 3 * 42 + 4 * 17 + 5 * 18 + 6 * 19 + 7 * 35) % 103 == 55
    # four or more digits go to set C in pairs; an odd leading digit stays in B
    v = code128_values("A12345")
    assert v == [104, 33, 17, 99, 23, 45, (104 + 33 + 2 * 17 + 3 * 99 + 4 * 23 + 5 * 45) % 103, 106]
    v = code128_values("1234")
    assert v[:3] == [105, 12, 34]
    # every symbol is 11 modules, the stop 13
    assert len(code128_modules(RACK)) == 11 * (len(code128_values(RACK)) - 1) + 13
    with pytest.raises(ValueError):
        code128_values("")
    with pytest.raises(ValueError, match="printable"):
        code128_values("tab\there")


def test_image_scale_and_quiet_zone():
    mods = code128_modules(RACK)
    im = code128_image(RACK, 120, 1180)
    scale = 1180 // (len(mods) + 20)
    assert scale >= 3 and im.size == ((len(mods) + 20) * scale, 120)
    assert im.getpixel((scale * 5, 60)) == 255  # inside the quiet zone
    assert im.getpixel((scale * 10, 60)) == 0  # the first bar of the start symbol
    with pytest.raises(ValueError, match="do not fit"):
        code128_image(RACK, 120, 300)
    with pytest.raises(ValueError, match="no height"):
        code128_image(RACK, 0, 1180)


def test_layouts_and_composer():
    w, h = 1180, 300  # a 4 × 1 in label at 300 dpi
    inter = bundled_fonts()["Inter Bold"]
    im = render_label(w, h, [RACK], inter, 100, "barcode", qr=RACK)
    assert im.size == (w, h) and im.getextrema() == (0, 255)
    # the bars take the upper part, the text sits below them
    assert im.crop((0, 0, w, h // 4)).getextrema() == (0, 255)
    assert im.crop((0, 3 * h // 4, w, h)).getextrema() == (0, 255)
    only = render_label(w, h, [""], inter, 100, "barcode-only", qr=RACK)
    assert only.size == (w, h) and only.crop((0, h - 20, w, h)).getextrema() == (0, 255)
    with pytest.raises(ValueError, match="needs QR"):
        render_label(w, h, [RACK], inter, 100, "barcode")
    reg = load(None)
    rows = "rack\nDH35.201/A\nDH35.201/B"  # the 2-up Panduit area is 436 dots: a short code for the test
    spec = Spec.from_dict(
        {
            "media": "panduit-s150x225vaty-2up",
            "font": "Inter Bold",
            "pt": 6,
            "layout": "barcode",
            "rows": rows,
            "template": "{rack}",
            "qr": "{rack}",
        }
    )
    assert spec.qrs == ["DH35.201/A", "DH35.201/B"]
    data, _, _, texts = render(reg, spec)
    assert texts == spec.qrs and rz.count_labels(data) == 1
    data, _, _, texts = render(
        reg,
        Spec.from_dict(
            {
                "media": "panduit-s150x225vaty-2up",
                "font": "Inter",
                "pt": 6,
                "layout": "barcode-only",
                "qr": "0042",
            }
        ),
    )
    assert texts == [""] and rz.count_labels(data) == 1


def test_round_trip_through_a_decoder():
    zx = pytest.importorskip("zxingcpp")
    for text in (RACK, "PJJ123C", "A12345", "1234", "x 1234567 y", "PHX2.1-DH35.201/A"):
        im = code128_image(text, 80, 1180)
        hits = zx.read_barcodes(im)
        assert [(str(h.format), h.text) for h in hits] == [("Code 128", text)], text
    # the layout's label decodes too, text and bars together
    inter = bundled_fonts()["Inter Bold"]
    lab = render_label(1180, 300, [RACK], inter, 90, "barcode", qr=RACK)
    assert [h.text for h in zx.read_barcodes(lab)] == [RACK]
