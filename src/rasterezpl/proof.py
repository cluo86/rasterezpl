"""A proof: the exact dots of a job drawn on the physical label, with a millimetre scale — what the labeller will
hold, at 1 dot = 1 pixel, in the physical frame (leading edge up), the way every label-design program shows it.
Nothing here is a stand-in: the bitmap is the decoded job page, so what the proof shows is what prints."""

from __future__ import annotations

from collections.abc import Callable

from .media import Media

MARGIN = 40  # px around the label for the scales


def proof_image(m: Media, page, overlay: Callable | None = None, caption: str = ""):
    """`page` = a decoded page bitmap in the PRINTER frame (decode_block); returns an RGB image in the PHYSICAL
    frame: paper (grey liner, white labels from `labels_in`, print areas outlined), the job's ink, a mm scale
    along the top and left edges, and `overlay(draw, to_px)` for stock-specific marks (die cuts, folds) where
    `to_px(x_in, y_in)` maps physical inches to pixels of the returned image."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = m.width_px, m.length_px
    phys = page.convert("L").rotate(180) if m.rotate180 else page.convert("L")
    img = Image.new("RGB", (w + 2 * MARGIN, h + 2 * MARGIN), "white")
    d = ImageDraw.Draw(img)
    ox, oy = MARGIN, MARGIN

    def to_px(x_in: float, y_in: float) -> tuple[int, int]:
        return ox + m.dots(x_in), oy + m.dots(y_in)

    # liner, labels, print areas
    d.rectangle([ox, oy, ox + w - 1, oy + h - 1], fill=(228, 226, 220), outline=(150, 150, 150))
    for x, y, lw, lh in m.labels_in:
        x0, y0 = to_px(x, y)
        x1, y1 = to_px(x + lw, y + lh)
        d.rounded_rectangle(
            [x0, y0, x1 - 1, y1 - 1], radius=m.dots(0.06), fill="white", outline=(120, 120, 120)
        )
    for k in range(len(m.areas_in)):
        x, y, aw, ah = m.area_px(k)
        d.rectangle([ox + x, oy + y, ox + x + aw - 1, oy + y + ah - 1], outline=(190, 190, 190))
    # ink: darken where the job prints
    ink = Image.new("RGB", (w, h), "black")
    mask = phys.point(lambda v: 255 - v)  # ink → 255
    img.paste(ink, (ox, oy), mask)
    # mm scales (physical frame): x along the top, y down the left; a number every 10 mm
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 11)
    except OSError:
        font = ImageFont.load_default()
    mm = m.dpi / 25.4
    for i in range(int(w / mm) + 1):
        x = ox + round(i * mm)
        ln = 12 if i % 10 == 0 else (7 if i % 5 == 0 else 3)
        d.line([x, oy - 2, x, oy - 2 - ln], fill="black")
        if i % 10 == 0:
            d.text((x + 2, oy - 2 - ln - 12), str(i), fill="black", font=font)
    for i in range(int(h / mm) + 1):
        y = oy + round(i * mm)
        ln = 12 if i % 10 == 0 else (7 if i % 5 == 0 else 3)
        d.line([ox - 2, y, ox - 2 - ln, y], fill="black")
        if i % 10 == 0:
            d.text((ox - 2 - ln - 22, y - 6), str(i), fill="black", font=font)
    if overlay is not None:
        overlay(d, to_px)
    text = (
        caption
        or f"{m.name} — {m.width_mm:.1f} × {m.length_mm:.1f} mm, 1 dot = 1 px at {m.dpi} dpi, leading edge up"
    )
    d.text((ox, oy + h + 6), text, fill=(80, 80, 80), font=font)
    return img
