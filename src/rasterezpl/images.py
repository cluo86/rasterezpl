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


def load_image(source: bytes | str | Path, width_px: int = 1200):
    """PNG / JPEG / SVG from bytes, a base64 string (optionally a data: URL) or a path → a Pillow 'L' image on
    white (transparency composited away). `width_px` is the raster width used for an SVG."""
    from PIL import Image

    if isinstance(source, str) and not Path(source).exists():
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
