"""Pictures on labels: loading, fitting to one bit, the picture layouts, the composer's image field."""

from __future__ import annotations

import base64
import io

import pytest

import rasterezpl as rz
from rasterezpl.compose import Spec, render
from rasterezpl.images import load_image, placeholder_logo, svg_renderer, to_bitmap
from rasterezpl.layouts import render_label
from rasterezpl.registry import load
from rasterezpl.text import bundled_fonts

PIL = pytest.importorskip("PIL")


def _png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_load_fit_and_modes(tmp_path):
    from PIL import Image

    mark = placeholder_logo(200)
    assert mark.size == (200, 200) and mark.getextrema() == (0, 255)
    # bytes, base64, data: URL and a path all load; alpha is composited on white
    rgba = Image.new("RGBA", (40, 20), (0, 0, 0, 0))
    rgba.paste((0, 0, 0, 255), (0, 0, 20, 20))
    png = _png_bytes(rgba)
    for src in (
        png,
        base64.b64encode(png).decode(),
        "data:image/png;base64," + base64.b64encode(png).decode(),
    ):
        im = load_image(src)
        assert im.size == (40, 20) and im.getpixel((5, 5)) == 0 and im.getpixel((35, 5)) == 255
    p = tmp_path / "m.png"
    p.write_bytes(png)
    assert load_image(p).size == (40, 20) and load_image(str(p)).size == (40, 20)
    with pytest.raises(ValueError):
        load_image(b"not an image")
    with pytest.raises(ValueError):
        load_image("nope nope")
    # fitting keeps the aspect and centres; threshold drops mid-grey, dither keeps some of it
    grey = Image.new("L", (100, 50), 160)
    bt = to_bitmap(grey, 300, 100, "threshold")
    bd = to_bitmap(grey, 300, 100, "dither")
    assert bt.size == bd.size == (300, 100) and bt.getextrema() == (255, 255) and bd.getextrema() == (0, 255)
    assert bt.getpixel((150, 50)) == 255
    with pytest.raises(ValueError):
        to_bitmap(grey, 10, 10, "nope")


def test_picture_layouts_and_composer():
    m = rz.PANDUIT_S150X225VATY_2UP
    _, _, w, h = m.area_px(0)
    inter = bundled_fonts()["Inter"]
    mark = placeholder_logo()
    for lay in ("logo", "logo-top", "image"):
        im = render_label(w, h, ["ASSET 1", "acme"], inter, 20, lay, image=mark)
        assert im.size == (w, h) and im.getextrema() == (0, 255)
        with pytest.raises(ValueError):
            render_label(w, h, ["x"], inter, 20, lay)  # no image
    # the picture sits left on `logo`: ink in the left 40 %, text (ink) to the right too
    im = render_label(w, h, ["ASSET 1"], inter, 20, "logo", image=mark)
    left = im.crop((0, 0, int(w * 0.4), h))
    right = im.crop((int(w * 0.4), 0, w, h))
    assert left.getextrema() == (0, 255) and right.getextrema() == (0, 255)
    reg = load(None)
    b64 = base64.b64encode(_png_bytes(mark)).decode()
    spec = Spec.from_dict(
        {
            "media": m.name and "panduit-s150x225vaty-2up",
            "font": "Inter Bold",
            "pt": 6,
            "layout": "logo",
            "text": "ASSET {n}\nacme",
            "image": b64,
            "copies": 2,
        }
    )
    data, mm, _, texts = render(reg, spec)
    assert texts == ["ASSET 1\nacme", "ASSET 1\nacme"] and rz.count_labels(data) == 1
    spec_img = Spec.from_dict(
        {"media": "panduit-s150x225vaty-2up", "font": "Inter", "pt": 6, "layout": "image", "image": b64}
    )
    data, _, _, texts = render(reg, spec_img)  # the image layout needs no text
    assert texts == [""] and rz.count_labels(data) == 1
    with pytest.raises(ValueError):
        render(
            reg,
            Spec.from_dict(
                {"media": "panduit-s150x225vaty-2up", "font": "Inter", "pt": 6, "layout": "logo", "text": "a"}
            ),
        )
    with pytest.raises(ValueError):
        render(reg, Spec.from_dict({**spec.__dict__, "image_mode": "nope"}))


def test_svg_when_a_renderer_exists():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 5"><rect x="0" y="0" width="5" height="5" fill="black"/></svg>'
    if svg_renderer() is None:
        with pytest.raises(ValueError, match="renderer"):
            load_image(svg, 100)
        return
    im = load_image(svg, 100)
    assert im.size[0] == 100 and im.getpixel((10, 10)) == 0 and im.getpixel((90, 10)) == 255
