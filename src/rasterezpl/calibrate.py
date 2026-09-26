"""One calibration label instead of a series of clipped test prints.

``ruler_job`` prints ticks every 10 dots and a number every 50 along both axes, in the PRINTER frame, across the
whole label; optionally over a decoded label page so the label's own text is read against the scale. Read where
the print-on edges fall: the x at the edge on the x = 0 side and the y at the trailing edge are the registration
to record for that printer (``Media``'s areas are assumed exact; if the two areas of a two-across web span a
different width than the media says, fix the media first — that was a pitch error, not a printer offset).
"""

from __future__ import annotations

from .media import Media
from .stream import label_block


def ruler_page(m: Media, font_path: str, over=None):
    from PIL import Image, ImageChops, ImageDraw, ImageFont

    w, h = m.width_px, m.length_px
    img = Image.new("L", (w, h), 255)
    if over is not None:
        if over.size != (w, h):
            raise ValueError(f"overlay {over.size} is not the page {(w, h)}")
        img = ImageChops.darker(img, over.convert("L"))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(font_path, max(10, m.dpi // 20))
    for y0 in (0, h // 2, h - 1):
        for x in range(0, w, 10):
            ln = 30 if x % 50 == 0 else 12
            d.line([x, y0, x, y0 + ln] if y0 < h - 1 else [x, y0 - ln, x, y0], fill=0)
            if x % 50 == 0:
                d.text((x + 2, y0 + 32 if y0 < h - 1 else y0 - 48), str(x), fill=0, font=f)
    for x0 in (0, w // 2, w - 1):
        for y in range(0, h, 10):
            ln = 30 if y % 50 == 0 else 12
            d.line([x0, y, x0 + ln, y] if x0 < w - 1 else [x0 - ln, y, x0, y], fill=0)
            if y % 50 == 0:
                d.text((x0 + 34 if x0 < w - 1 else x0 - 70, y - 6), str(y), fill=0, font=f)
    d.text((w // 2 - 120, h // 2 + 60), f"{m.name}: x → , y ↓ (printer frame)", fill=0, font=f)
    return img


def ruler_job(m: Media, font_path: str, over=None) -> bytes:
    """The ruler as one label block (unrotated, no offset — readings are the whole correction)."""
    return label_block(m.raw_page(), [ruler_page(m, font_path, over)])
