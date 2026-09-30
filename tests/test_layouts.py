"""Bundled faces and label layouts: every layout renders its sample in every bundled face, with ink, inside the
area; the composer takes a layout; the demo ships the template sheet."""

from __future__ import annotations

import pytest

import rasterezpl as rz
from rasterezpl.compose import Spec, render
from rasterezpl.layouts import LAYOUTS, render_label
from rasterezpl.registry import load
from rasterezpl.text import bundled_fonts, find_font

PIL = pytest.importorskip("PIL")


def test_bundled_fonts_resolve_by_name():
    fonts = bundled_fonts()
    assert set(fonts) == {"Inter", "Inter Bold", "JetBrains Mono", "Bebas Neue"}
    for name, path in fonts.items():
        assert path.endswith(".ttf") and find_font(name) == path and find_font(name.lower()) == path
    assert find_font("No Such Face") is None


def test_every_layout_renders_its_sample_in_every_face():
    m = rz.PANDUIT_S150X225VATY_2UP
    _, _, w, h = m.area_px(0)
    from rasterezpl.images import placeholder_logo

    mark = placeholder_logo()
    for lay in LAYOUTS.values():
        for face, path in bundled_fonts().items():
            px = rz.pt_to_px(6 if lay.name in ("banner", "ticket") else 5, m.dpi)
            im = render_label(
                w,
                h,
                lay.sample.split("\n"),
                path,
                px,
                lay.name,
                image=mark if lay.needs_image else None,
                qr="C3000071A605" if lay.needs_qr else None,
            )
            assert im.size == (w, h) and im.getextrema() == (0, 255), (lay.name, face)
    # a banner's band is black at the top edge, a plain label is white there
    inter = bundled_fonts()["Inter"]
    band = render_label(w, h, ["HEAD", "body"], inter, 20, "banner")
    plain = render_label(w, h, ["HEAD", "body"], inter, 20, "plain")
    assert band.getpixel((2, 2)) == 0 and plain.getpixel((2, 2)) == 255
    with pytest.raises(ValueError):
        render_label(w, h, ["x"], inter, 20, "nope")


def test_compose_spec_with_layout_and_bundled_face(tmp_path):
    reg = load(None)  # presets only
    spec = Spec.from_dict(
        {
            "media": "panduit-s150x225vaty-2up",
            "font": "Bebas Neue",
            "pt": 7,
            "layout": "ticket",
            "text": "TICKET {n}\nkeep this",
        }
    )
    assert spec.layout == "ticket"
    data, m, font, texts = render(reg, spec)
    assert (
        font.endswith("BebasNeue-Regular.ttf")
        and texts == ["TICKET 1\nkeep this"]
        and rz.count_labels(data) == 1
    )
    with pytest.raises(ValueError):
        render(
            reg,
            Spec.from_dict(
                {"media": "panduit-s150x225vaty-2up", "font": "Inter", "pt": 5, "layout": "zzz", "text": "a"}
            ),
        )


def test_demo_ships_the_template_sheet(tmp_path):
    from rasterezpl.demo import build

    info = build(tmp_path / "d")
    assert "templates/layouts.ezpl" in info["written"]
    data = (tmp_path / "d" / "templates" / "layouts.ezpl").read_bytes()
    assert rz.count_labels(data) == (len(LAYOUTS) + 1) // 2  # two layouts per web row
