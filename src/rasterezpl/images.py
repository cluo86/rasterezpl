"""Pictures on labels — a logo for asset tags, an icon, a QR someone else made: any PNG/JPEG, or an SVG when a
renderer is available, fitted into a box and turned into the one-bit image a thermal head prints.

Vector input: an SVG is rasterised at the box's width by ``cairosvg`` (a Python package) when importable, else
by ``rsvg-convert`` (librsvg, on the PATH) when installed — the package does not bundle a renderer. Without
either, an SVG is refused with a message saying so; PNG and JPEG always work (Pillow).

One-bit conversion: ``dither`` (the default) keeps greys as a halftone (Floyd–Steinberg); ``threshold`` keeps
only what is darker than mid-grey — crisper for a mark that is already two-tone.
"""

from __future__ import annotations

import base64
import io
import shutil
import subprocess
from pathlib import Path

MODES = ("dither", "threshold")


def _is_svg(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower())


def svg_renderer() -> str | None:
    """'cairosvg', 'rsvg-convert', or None — what this machine can rasterise an SVG with."""
    try:
        import cairosvg  # noqa: F401

        return "cairosvg"
    except ImportError:
        pass
    return "rsvg-convert" if shutil.which("rsvg-convert") else None


def rasterize_svg(data: bytes, width_px: int):
    """An SVG as a Pillow RGBA image `width_px` wide (height by its aspect)."""
    from PIL import Image

    tool = svg_renderer()
    if tool == "cairosvg":
        import cairosvg

        png = cairosvg.svg2png(bytestring=data, output_width=width_px)
        return Image.open(io.BytesIO(png)).convert("RGBA")
    if tool == "rsvg-convert":
        r = subprocess.run(
            ["rsvg-convert", "-w", str(width_px), "-f", "png"], input=data, capture_output=True, check=False
        )
        if r.returncode != 0:
            raise ValueError(f"rsvg-convert failed: {r.stderr.decode(errors='replace').strip()}")
        return Image.open(io.BytesIO(r.stdout)).convert("RGBA")
    raise ValueError("an SVG needs a renderer: pip install cairosvg, or install librsvg (rsvg-convert)")


def _is_path(s: str) -> bool:
    """A string names a file only if it is short enough to be one and exists — a base64 blob is neither (and
    on Linux `Path(blob).exists()` raises 'File name too long')."""
    if len(s) > 1024 or "\n" in s:
        return False
    try:
        return Path(s).exists()
    except OSError:
        return False


def load_image(source: bytes | str | Path, width_px: int = 1200):
    """PNG / JPEG / SVG from bytes, a base64 string (optionally a data: URL) or a path → a Pillow 'L' image on
    white (transparency composited away). `width_px` is the raster width used for an SVG."""
    from PIL import Image

    if isinstance(source, str) and not _is_path(source):
        s = source.split(",", 1)[1] if source.startswith("data:") else source
        try:
            data = base64.b64decode(s, validate=False)
        except Exception as e:
            raise ValueError("image: not a file path and not base64") from e
    elif isinstance(source, (str, Path)):
        data = Path(source).read_bytes()
    else:
        data = source
    if not data:
        raise ValueError("image: empty")
    if _is_svg(data):
        im = rasterize_svg(data, width_px)
    else:
        try:
            im = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception as e:
            raise ValueError(f"image: not a PNG/JPEG/SVG ({e})") from e
    bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
    bg.alpha_composite(im)
    return bg.convert("L")


def to_bitmap(img, w: int, h: int, mode: str = "dither", threshold: int = 128):
    """Fit `img` inside (w, h) dots keeping its aspect, centred on white, as a one-bit 'L' image (0 ink / 255)."""
    from PIL import Image

    if mode not in MODES:
        raise ValueError(f"image mode {mode!r}: one of {', '.join(MODES)}")
    if w <= 0 or h <= 0:
        raise ValueError("no room for the image")
    scale = min(w / img.width, h / img.height)
    tw, th = max(1, round(img.width * scale)), max(1, round(img.height * scale))
    fitted = img.resize((tw, th), Image.Resampling.LANCZOS)
    if mode == "dither":
        one = fitted.convert("1")  # Floyd–Steinberg
    else:
        one = fitted.point(lambda p: 0 if p < threshold else 255).convert("1")
    out = Image.new("L", (w, h), 255)
    out.paste(one.convert("L"), ((w - tw) // 2, (h - th) // 2))
    return out


def placeholder_logo(size: int = 480):
    """A generic mark to stand in for a logo: a hexagon with a triangle — the demo's asset tag uses it. Replace
    with your own PNG or SVG; this one is drawn here so no image file ships with the package."""
    import math

    from PIL import Image, ImageDraw

    im = Image.new("L", (size, size), 255)
    d = ImageDraw.Draw(im)
    c, r = size / 2, size * 0.46
    hexagon = [
        (c + r * math.cos(math.radians(60 * k - 30)), c + r * math.sin(math.radians(60 * k - 30)))
        for k in range(6)
    ]
    d.polygon(hexagon, outline=0, width=max(2, size // 24))
    r2 = r * 0.55
    tri = [
        (c + r2 * math.cos(math.radians(120 * k - 90)), c + r2 * math.sin(math.radians(120 * k - 90)))
        for k in range(3)
    ]
    d.polygon(tri, fill=0)
    return im


def qr_image(text: str, box_px: int, error: str = "m", border: int = 2):
    """A QR code for `text` as a one-bit Pillow 'L' image, the largest whole-module scale that fits `box_px`
    square (standard QR, never Micro; error level L/M/Q/H, default M; `border` = quiet zone in modules)."""
    import segno
    from PIL import Image

    if not text:
        raise ValueError("qr: nothing to encode")
    q = segno.make(text, error=error, micro=False)
    n = int(q.symbol_size(scale=1, border=0)[0])
    scale = box_px // (n + 2 * border)
    if scale < 1:
        raise ValueError(f"qr: {n} modules do not fit {box_px} dots even at one dot per module")
    side = (n + 2 * border) * scale
    im = Image.new("L", (side, side), 255)
    px = im.load()
    assert px is not None
    for y, row in enumerate(q.matrix):
        for x, dark in enumerate(row):
            if dark:
                x0, y0 = (x + border) * scale, (y + border) * scale
                for yy in range(y0, y0 + scale):
                    for xx in range(x0, x0 + scale):
                        px[xx, yy] = 0
    return im


# --- Code 128 ------------------------------------------------------------------------------------------------------
# The 107 symbol patterns as bar/space module widths (bar, space, bar, space, bar, space; the stop adds a bar):
# ISO/IEC 15417. A symbol is 11 modules wide, the stop 13; a quiet zone of 10 modules each side is the minimum.
_CODE128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 "
    "113222 123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 "
    "113123 113321 133121 313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 "
    "241211 221114 413111 241112 134111 111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 114131 311141 411131 211412 211214 "
    "211232 2331112"
).split()
_CODE_C, _CODE_B, _START_B, _START_C, _STOP = 99, 100, 104, 105, 106
CODE128_QUIET = 10  # modules of quiet zone each side


def code128_values(text: str) -> list[int]:
    """The Code 128 symbol values for `text` (start, data, checksum, stop): code set B for printable ASCII, code
    set C for runs of four or more digits (pairs; an odd leading digit stays in B) — the usual compaction. Control
    characters (code set A) are refused: a label's human-readable text never carries them."""
    if not text:
        raise ValueError("code128: nothing to encode")
    bad = [c for c in text if not 32 <= ord(c) <= 126]
    if bad:
        raise ValueError(f"code128: not printable ASCII: {bad[0]!r} (code set B only)")
    vals: list[int] = []
    cur = None
    i = 0
    while i < len(text):
        run = 0
        while i + run < len(text) and text[i + run].isdigit():
            run += 1
        if run >= 4:
            if run % 2:  # an odd run: the pairs are taken from the end, the leading digit goes in set B
                if cur is None:
                    vals.append(_START_B)
                elif cur != "B":
                    vals.append(_CODE_B)
                cur = "B"
                vals.append(ord(text[i]) - 32)
                i += 1
                run -= 1
            if cur is None:
                vals.append(_START_C)
            elif cur != "C":
                vals.append(_CODE_C)
            cur = "C"
            while run >= 2:
                vals.append(int(text[i : i + 2]))
                i += 2
                run -= 2
            continue
        if cur is None:
            vals.append(_START_B)
        elif cur != "B":
            vals.append(_CODE_B)
        cur = "B"
        vals.append(ord(text[i]) - 32)
        i += 1
    check = (vals[0] + sum(k * v for k, v in enumerate(vals[1:], 1))) % 103
    return [*vals, check, _STOP]


def code128_modules(text: str) -> str:
    """The symbol as a string of '1' (bar) / '0' (space) modules, no quiet zone."""
    out = []
    for v in code128_values(text):
        ink = True
        for wdt in _CODE128[v]:
            out.append(("1" if ink else "0") * int(wdt))
            ink = not ink
    return "".join(out)


def code128_image(
    text: str, height_px: int, max_width_px: int, quiet: int = CODE128_QUIET, min_module: int = 2
):
    """A Code 128 barcode for `text` as a one-bit Pillow 'L' image `height_px` tall, bars at the widest whole
    number of dots per module that fits `max_width_px` with the quiet zones (`quiet` modules each side); refused
    below `min_module` dots per module (2 at 300 dpi = 0.17 mm, about the least a hand scanner reads)."""
    from PIL import Image, ImageDraw

    mods = code128_modules(text)
    n = len(mods) + 2 * quiet
    scale = max_width_px // n
    if scale < min_module:
        raise ValueError(
            f"code128: {len(mods)} modules (+{2 * quiet} quiet) do not fit {max_width_px} dots at "
            f"{min_module} dots per module (would need {n * min_module})"
        )
    if height_px <= 0:
        raise ValueError("code128: no height for the bars")
    im = Image.new("L", (n * scale, height_px), 255)
    d = ImageDraw.Draw(im)
    x = quiet * scale
    for m in mods:
        if m == "1":
            d.rectangle((x, 0, x + scale - 1, height_px - 1), fill=0)
        x += scale
    return im
