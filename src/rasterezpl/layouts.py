"""Label layouts — a decoration around the text, drawn in dots, then the text rendered into what is left.

A layout is a function of the print area (w, h dots), the lines, the face and size: it paints its pattern and
returns the label image. The text itself is always :func:`rasterezpl.text.render_text` into an inner box, so the
no-clip / fit rules hold inside every layout. Each layout carries a SAMPLE text so a template can be shown filled.

    plain     the text, nothing else (the default)
    framed    a rounded double rule around the text
    banner    the first line white-on-black in a band along the top, the rest below
    sidebar   a striped bar down the left edge, the text beside it
    corners   corner marks, like a crop-marked card
    ticket    a dashed rule with a "tear here" gap between the first line and the rest
    logo      a picture on the left (about 40 % of the width), the text beside it — asset tags
    logo-top  the picture across the top (about 45 % of the height), the text below
    image     the picture alone, fitted to the label

    clear-qr    for a self-laminating label printed WHOLE (media *-full): the lines on the white block (the leading
                third), the QR as large as fits on the clear two thirds — a flat-stuck asset tag or badge
    clear-image the same with a picture on the clear part
    badge     a QR code across the top (square, as large as fits), the lines centred below — check-in badges
    qr-left   the QR on the left, the lines beside it — narrow stock
    qr        the QR alone

The three picture layouts take an ``image`` (a Pillow 'L' image, see :mod:`rasterezpl.images`) and refuse without
one; the QR layouts take ``qr`` (the text to encode) and refuse without it; the others ignore both.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .text import render_text


@dataclass(frozen=True)
class Layout:
    name: str
    sample: str  # lines joined by "\n"
    draw: Callable  # (w, h, lines, font, px, align, fit, image=None, qr=None) -> PIL image
    needs_image: bool = False
    needs_qr: bool = False


def _canvas(w: int, h: int):
    from PIL import Image, ImageDraw

    img = Image.new("L", (w, h), 255)
    return img, ImageDraw.Draw(img)


def _text_into(img, box: tuple[int, int, int, int], lines, font, px, align, fit, invert=False):
    """Render the lines into box=(x, y, w, h) of img; `invert` = white text on the black box."""
    from PIL import ImageOps

    x, y, bw, bh = box
    if bw <= 0 or bh <= 0 or not lines:
        return img
    t = render_text((bw, bh), lines, font, px, align=align, fit=fit)
    if invert:
        t = ImageOps.invert(t)
    img.paste(t, (x, y))
    return img


def plain(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    return render_text((w, h), lines, font, px, align=align, fit=fit)


def framed(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    img, d = _canvas(w, h)
    m = max(2, min(w, h) // 30)
    r = max(4, min(w, h) // 12)
    d.rounded_rectangle((m, m, w - 1 - m, h - 1 - m), radius=r, outline=0, width=max(1, m // 2))
    m2 = m * 3
    d.rounded_rectangle((m2, m2, w - 1 - m2, h - 1 - m2), radius=max(2, r - m2 // 2), outline=0, width=1)
    pad = m2 + max(2, m)
    return _text_into(img, (pad, pad, w - 2 * pad, h - 2 * pad), lines, font, px, align, fit)


def banner(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    img, d = _canvas(w, h)
    band = int(px * 1.6) + 4
    band = min(band, h // 2)
    d.rectangle((0, 0, w - 1, band - 1), fill=0)
    head, rest = lines[:1], lines[1:]
    _text_into(img, (2, 1, w - 4, band - 2), head, font, px, align, True, invert=True)
    pad = max(2, band // 6)
    return _text_into(img, (pad, band + pad, w - 2 * pad, h - band - 2 * pad), rest, font, px, align, fit)


def sidebar(w, h, lines, font, px, align="left", fit=False, image=None, qr=None):
    img, d = _canvas(w, h)
    bar = max(6, w // 14)
    period = max(4, bar // 2)
    for y in range(-bar, h + bar, period):
        d.line((0, y, bar, y + bar), fill=0, width=max(1, period // 3))
    d.rectangle((0, 0, bar, h - 1), outline=0, width=1)
    pad = max(2, bar // 3)
    return _text_into(img, (bar + pad * 2, pad, w - bar - pad * 3, h - 2 * pad), lines, font, px, align, fit)


def corners(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    img, d = _canvas(w, h)
    L = max(6, min(w, h) // 6)
    t = max(1, min(w, h) // 60)
    for x0, y0, dx, dy in ((0, 0, 1, 1), (w - 1, 0, -1, 1), (0, h - 1, 1, -1), (w - 1, h - 1, -1, -1)):
        d.line((x0, y0, x0 + dx * L, y0), fill=0, width=t)
        d.line((x0, y0, x0, y0 + dy * L), fill=0, width=t)
    pad = max(3, t * 2 + 2)
    return _text_into(img, (pad, pad, w - 2 * pad, h - 2 * pad), lines, font, px, align, fit)


def ticket(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    img, d = _canvas(w, h)
    head, rest = lines[:1], lines[1:]
    top = int(px * 1.5) + 4 if rest else h
    top = min(top, h // 2) if rest else h
    _text_into(img, (2, 1, w - 4, top - 2), head, font, px, align, True)
    if rest:
        dash = max(3, w // 60)
        for x in range(2, w - 2, dash * 2):
            d.line((x, top, min(x + dash, w - 3), top), fill=0, width=1)
        # the scissors: a small notch at each end of the rule
        d.ellipse((0, top - 3, 6, top + 3), outline=0)
        d.ellipse((w - 7, top - 3, w - 1, top + 3), outline=0)
        pad = 3
        _text_into(img, (pad, top + pad, w - 2 * pad, h - top - 2 * pad), rest, font, px, align, fit)
    return img


def _need(image, name):
    if image is None:
        raise ValueError(f"layout {name!r} needs an image (a PNG, JPEG or SVG)")
    return image


def logo(w, h, lines, font, px, align="left", fit=False, image=None, qr=None):
    from .images import to_bitmap

    img = _need(image, "logo")
    out, _ = _canvas(w, h)
    pad = max(3, min(w, h) // 30)
    lw = int(w * 0.4)
    out.paste(to_bitmap(img, lw - 2 * pad, h - 2 * pad), (pad, pad))
    return _text_into(out, (lw + pad, pad, w - lw - 2 * pad, h - 2 * pad), lines, font, px, align, fit)


def logo_top(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    from .images import to_bitmap

    img = _need(image, "logo-top")
    out, _ = _canvas(w, h)
    pad = max(3, min(w, h) // 30)
    lh = int(h * 0.45)
    out.paste(to_bitmap(img, w - 2 * pad, lh - pad), (pad, pad))
    return _text_into(out, (pad, lh + pad, w - 2 * pad, h - lh - 2 * pad), lines, font, px, align, fit)


def image_only(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    from .images import to_bitmap

    return to_bitmap(_need(image, "image"), w, h)


def _need_qr(qr, name):
    if not qr:
        raise ValueError(f"layout {name!r} needs QR data (the text to encode)")
    return qr


def badge(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    from .images import qr_image

    data = _need_qr(qr, "badge")
    out, _ = _canvas(w, h)
    pad = max(3, min(w, h) // 30)
    text_h = int(px * 1.3 * len(lines)) + pad if lines and any(lines) else 0
    box = min(w - 2 * pad, h - 2 * pad - text_h)
    code = qr_image(data, box)
    out.paste(code, ((w - code.width) // 2, pad + (box - code.height) // 2))
    top = pad + box + pad
    return _text_into(out, (pad, top, w - 2 * pad, h - top - pad), lines, font, px, align, fit)


def qr_left(w, h, lines, font, px, align="left", fit=False, image=None, qr=None):
    from .images import qr_image

    data = _need_qr(qr, "qr-left")
    out, _ = _canvas(w, h)
    pad = max(3, min(w, h) // 30)
    box = h - 2 * pad
    code = qr_image(data, box)
    out.paste(code, (pad, pad + (box - code.height) // 2))
    left = pad + box + pad * 2
    return _text_into(out, (left, pad, w - left - pad, h - 2 * pad), lines, font, px, align, fit)


def qr_only(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    from .images import qr_image

    out, _ = _canvas(w, h)
    code = qr_image(_need_qr(qr, "qr"), min(w, h))
    out.paste(code, ((w - code.width) // 2, (h - code.height) // 2))
    return out


WHITE_FRAC = 0.75 / 2.25  # the print-on block of the S150X225VATY: the leading third of the label


def _clear(w, h, lines, font, px, align, fit, picture):
    """Text on the white third, `picture` (a one-bit image already fitted) centred on the clear two thirds."""
    out, _ = _canvas(w, h)
    white_h = int(h * WHITE_FRAC)
    pad = max(3, min(w, h) // 40)
    _text_into(out, (pad, pad, w - 2 * pad, white_h - 2 * pad), lines, font, px, align, fit)
    out.paste(picture, ((w - picture.width) // 2, white_h + (h - white_h - picture.height) // 2))
    return out


def clear_qr(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    from .images import qr_image

    white_h = int(h * WHITE_FRAC)
    pad = max(3, min(w, h) // 40)
    box = min(w, h - white_h) - 2 * pad
    return _clear(w, h, lines, font, px, align, fit, qr_image(_need_qr(qr, "clear-qr"), box))


def clear_image(w, h, lines, font, px, align="center", fit=False, image=None, qr=None):
    from .images import to_bitmap

    white_h = int(h * WHITE_FRAC)
    pad = max(3, min(w, h) // 40)
    pic = to_bitmap(_need(image, "clear-image"), w - 2 * pad, h - white_h - 2 * pad)
    return _clear(w, h, lines, font, px, align, fit, pic)


LAYOUTS: dict[str, Layout] = {
    "plain": Layout("plain", "rasterezpl\nplain text\nthe default", plain),
    "framed": Layout("framed", "FRAMED\ndouble rule\nrounded corners", framed),
    "banner": Layout("banner", "PATCH PANEL A", banner),
    "sidebar": Layout("sidebar", "sidebar\nstriped bar left\ntext beside it", sidebar),
    "corners": Layout("corners", "CORNERS\ncrop-marked card", corners),
    "ticket": Layout("ticket", "TICKET 0042\nkeep this half\ntear along the rule", ticket),
}
# banners want a second line under the band in the sample
LAYOUTS["banner"] = Layout("banner", "PATCH PANEL A\nrack 12 · U31\n24 × LC duplex", banner)
LAYOUTS["logo"] = Layout("logo", "ASSET 0042\nproperty of ACME\nreturn if found", logo, needs_image=True)
LAYOUTS["logo-top"] = Layout("logo-top", "ACME\nrack 12 · U31", logo_top, needs_image=True)
LAYOUTS["image"] = Layout("image", "", image_only, needs_image=True)
LAYOUTS["badge"] = Layout("badge", "Jane Doe\nC3000071A605\nAcme Fibre", badge, needs_qr=True)
LAYOUTS["qr-left"] = Layout("qr-left", "Jane Doe\nC3000071A605", qr_left, needs_qr=True)
LAYOUTS["qr"] = Layout("qr", "", qr_only, needs_qr=True)
LAYOUTS["clear-qr"] = Layout("clear-qr", "Jane Doe\nC3000071A605\nAcme Fibre", clear_qr, needs_qr=True)
LAYOUTS["clear-image"] = Layout("clear-image", "ASSET 0042\nproperty of ACME", clear_image, needs_image=True)


def render_label(
    w: int,
    h: int,
    lines: list[str],
    font: str,
    px: int,
    layout: str = "plain",
    align: str = "center",
    fit: bool = False,
    image=None,
    qr: str | None = None,
):
    """One label image of (w, h) dots in the named layout; `image` = a Pillow 'L' image for the picture layouts,
    `qr` = the text the QR layouts encode."""
    try:
        lay = LAYOUTS[layout]
    except KeyError as e:
        raise ValueError(f"unknown layout {layout!r}; one of {', '.join(LAYOUTS)}") from e
    return lay.draw(w, h, lines, font, px, align, fit, image=image, qr=qr)
