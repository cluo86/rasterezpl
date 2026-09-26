"""Text rendered on the host with a real font — the reason to raster at all: the label was proven on the stock in
a specific face and size, and the printer's resident fonts are neither."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

FONT_CANDIDATES = {
    "arial": (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/msttcorefonts/Arial.ttf",
    ),
    "microsoft sans serif": (
        "/System/Library/Fonts/Supplemental/Microsoft Sans Serif.ttf",
        "C:/Windows/Fonts/micross.ttf",
    ),
    "dejavu sans": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/TTF/DejaVuSans.ttf",
        "/opt/homebrew/share/fonts/DejaVuSans.ttf",
    ),
}


def find_font(*names: str) -> str | None:
    """The first of the named faces present on this machine, by its known paths; a path is accepted as is.
    Returns None rather than a substitute: a different face prints a different label."""
    for name in names:
        if Path(name).exists():
            return name
        for cand in FONT_CANDIDATES.get(name.lower(), ()):
            if Path(cand).exists():
                return cand
    return None


def render_text(
    size: tuple[int, int],
    lines: Sequence[str],
    font_path: str,
    px: int,
    align: str = "center",
    line_spacing: float = 1.2,
    rotate180: bool = False,
    margin: int = 0,
    fit: bool = False,
):
    """Lines of text on a white image of ``size`` dots, ``px`` = font size in dots, lines centred vertically.
    A line wider than the image (minus margins) is an error, never clipped — unless ``fit``, which shrinks the font
    until the longest line fits (and raises when even 6 dots per em does not)."""
    from PIL import Image, ImageDraw, ImageFont

    w, h = size
    while True:
        font = ImageFont.truetype(font_path, px)
        widths = [font.getlength(t) for t in lines]
        lp = round(px * line_spacing)
        if max(widths, default=0) <= w - 2 * margin and lp * len(lines) <= h - 2 * margin:
            break
        if not fit or px <= 6:
            raise ValueError(
                f"text does not fit {w}×{h} dots at {px} dots/em: widest line {max(widths, default=0):.0f} dots "
                f"({lines[widths.index(max(widths))]!r}), {len(lines)} lines × {lp} = {lp * len(lines)} dots"
            )
        px -= 1
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    y = (h - lp * len(lines)) / 2
    for t, tw in zip(lines, widths, strict=True):
        x = margin if align == "left" else (w - tw) / 2
        d.text((x, y), t, fill=0, font=font)
        y += lp
    if rotate180:
        img = img.rotate(180)
    return img


def pt_to_px(pt: float, dpi: int) -> int:
    return round(pt * dpi / 72)
