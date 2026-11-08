"""Text rendered on the host with a real font — the reason to raster at all: the label was proven on the stock in
a specific face and size, and the printer's resident fonts are neither."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path

FONT_CANDIDATES = {
    "arial": (
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/Library/Fonts/Arial.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "~/AppData/Local/Microsoft/Windows/Fonts/arial.ttf",  # a per-user install (Windows 10+)
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


def bundled_fonts() -> dict[str, str]:
    """The faces shipped with the package (SIL OFL, see fonts/LICENSES.md): display name → path. They render the
    same on every machine, which is the point: Inter, Inter Bold, JetBrains Mono, Bebas Neue."""
    from importlib import resources

    out: dict[str, str] = {}
    d = resources.files("rasterezpl").joinpath("fonts")
    for f in sorted(d.iterdir(), key=lambda x: x.name):
        if f.name.lower().endswith((".ttf", ".otf")):
            stem = f.name.rsplit(".", 1)[0]
            name = {
                "Inter-Regular": "Inter",
                "Inter-Bold": "Inter Bold",
                "JetBrainsMono-Regular": "JetBrains Mono",
                "BebasNeue-Regular": "Bebas Neue",
            }.get(stem, stem)
            out[name] = str(f)
    return out


def font_dirs() -> list[Path]:
    """Where this OS keeps its fonts, the folders that exist: Windows ``%WINDIR%\\Fonts`` and the per-user
    ``%LOCALAPPDATA%\\Microsoft\\Windows\\Fonts`` (Windows 10+ installs a user's fonts there); macOS the system,
    Supplemental, local and user Library folders; Linux the share and user font folders."""
    if sys.platform == "win32":
        dirs = [Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs = [
            Path("/System/Library/Fonts"),
            Path("/System/Library/Fonts/Supplemental"),
            Path("/Library/Fonts"),
            Path("~/Library/Fonts").expanduser(),
        ]
    else:
        dirs = [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path("~/.fonts").expanduser(),
            Path("~/.local/share/fonts").expanduser(),
        ]
    return [d for d in dirs if d.is_dir()]


@lru_cache(maxsize=1)
def system_fonts() -> dict[str, str]:
    """The faces installed on this machine: display name → path for every ``.ttf`` / ``.otf`` / ``.ttc`` under
    :func:`font_dirs` (recursively), named as the font names itself — family, plus the style unless Regular
    ("Arial", "Arial Bold", "Arial Narrow") — read with Pillow, no font registry consulted (Windows' registry
    and fontconfig are not needed). A file Pillow cannot open is skipped; the first file for a name wins.
    Computed once per process."""
    from PIL import ImageFont

    out: dict[str, str] = {}
    for d in font_dirs():
        for f in sorted(d.rglob("*")):
            if f.suffix.lower() not in (".ttf", ".otf", ".ttc") or not f.is_file():
                continue
            try:
                family, style = ImageFont.truetype(str(f), 10, layout_engine=ImageFont.Layout.BASIC).getname()
            except Exception:
                continue
            family = str(family or f.stem)
            name = (
                family
                if not style or style.lower() in ("regular", "normal", "book", "roman")
                else f"{family} {style}"
            )
            out.setdefault(name, str(f))
    return out


def find_font(*names: str) -> str | None:
    """The first of the named faces present: a path as is, a bundled face (see `bundled_fonts`), a face known by
    its usual paths on this machine, or one of :func:`system_fonts` by its name (case-insensitive). Returns None
    rather than a substitute: a different face prints a different label."""
    bundled = {k.lower(): v for k, v in bundled_fonts().items()}
    for name in names:
        if Path(name).exists():
            return name
        if name.lower() in bundled:
            return bundled[name.lower()]
        for cand in FONT_CANDIDATES.get(name.lower(), ()):
            if Path(cand).expanduser().exists():
                return str(Path(cand).expanduser())
    installed = {k.lower(): v for k, v in system_fonts().items()}
    for name in names:
        if name.lower() in installed:
            return installed[name.lower()]
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
    stroke: int = 0,
):
    """Lines of text on a white image of ``size`` dots, ``px`` = font size in dots, lines centred vertically.
    A line wider than the image (minus margins) is an error, never clipped — unless ``fit``, which shrinks the font
    until the longest line fits (and raises when even 6 dots per em does not). ``stroke`` = extra dots of ink
    around every glyph outline (1 or 2 thickens any face for a head that drops thin strokes)."""
    from PIL import Image, ImageDraw

    w, h = size
    stroke = max(0, int(stroke))
    while True:
        font = load_font(font_path, px)
        widths = [font.getlength(t) + 2 * stroke for t in lines]
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
        d.text((x + stroke, y), t, fill=0, font=font, stroke_width=stroke, stroke_fill=0)
        y += lp
    if rotate180:
        img = img.rotate(180)
    return img


def load_font(path: str, px: int):
    """A face at ``px`` dots per em, laid out by FreeType's BASIC engine on every machine. Pillow picks the raqm
    engine (HarfBuzz shaping and kerning) whenever libfribidi happens to be installed — a Mac with Homebrew, an
    Ubuntu box — and the basic one elsewhere (a Windows laptop, a bare macOS), and the two measure the same line
    differently: Inter Bold at 100 dots/em set "IAD108.1 SC77 R6225/A" 1162 dots wide basic and 1150.9 raqm
    (2026-11-08, found by the CI matrix). One raster everywhere is the promise, so the engine is pinned; the
    basic engine is inside every Pillow wheel and needs no library. Kerning is lost — the same loss on every
    machine, and a label is proven as it prints."""
    from PIL import ImageFont

    return ImageFont.truetype(path, max(1, int(px)), layout_engine=ImageFont.Layout.BASIC)


def pt_to_px(pt: float, dpi: int) -> int:
    return round(pt * dpi / 72)
