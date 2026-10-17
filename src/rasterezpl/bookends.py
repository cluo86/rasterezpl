"""Bookends: a START and an END label around a job, on the job's own media, in the job's own stream.

A crew tearing strips off a roll of hundreds of cable labels cannot tell from the paper where one job ends and
the next begins, nor whether a job completed — the last label printed looks like every other, and the only
record is the print log on the sending machine (2026-10-16, a 128-label job on a Panduit TDP43ME: the crew held
the last label on the roll and asked whether the job had finished). The bookends put that on the paper: a START
block in front of the labels and an END block after them, in the SAME transport write as the labels — so an END
label in the hand is proof the whole stream arrived, and a missing one means the job was cut.

They are our work tags, not customer labels: free text, no label grammar, and a look no cable label carries —
the whole print area black, a white border inset a millimetre, hazard-stripe bands along the top and bottom
edges, the word START / END in the largest bold face that fits, and the END label cut in two by a white
"CUT HERE" band. Label stock is dear and ink is cheap: exactly two blocks per job, every print area of a block
carrying the mark (a two-across web spends two labels per bookend — one row).

Since 0.18.0 a mark covers the WHOLE die-cut label when the media knows its outline (``labels_in``) and that is
larger than the print area — the Panduit self-laminating stock: the word stays on the white print-on block, the
details (file, labels, count, printer, time; the END legend) go on the clear tail in black, as large as they
fit, with a hazard band at the label's far end. The tail takes thermal-transfer ink like the block does and on
a work tag it is wasted otherwise (2026-10-17: the white-on-black details on the 0.75 in block alone were
barely legible on the roll). A media without ``labels_in``, or whose label is its print area, keeps the
block-only mark, byte for byte.

The blocks are built from the job's own first setup (the same ``^Q``/``^W``/heat as its labels), so they pass
the media match the labels pass; the tail ink lies outside the print areas by design (a bookend block is never
checked against them — it is not a label), and the printer's darkness / speed retune at send time applies to
them like any block. They are added AFTER the label selection: ``--labels`` picks from
the file, never from the bookends, and the count of labels never includes them.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from .media import EOL, Media
from .stream import LabelBlock, parse_blocks, pattern_blocks

if TYPE_CHECKING:
    from PIL import ImageFont

TIMESTAMP = "%Y-%m-%d %H:%M"  # local, the clock of the print log's ``ts``
BORDER_INSET_MM = 1.0
BORDER_MM = 0.35
HATCH_MM = 2.2  # the hazard bands along the top and bottom edges
STRIPE_MM = 1.4  # one white stripe of the hatch, and the gap between stripes


def now() -> str:
    """The timestamp the bookends carry — the print log's clock, to the minute."""
    return time.strftime(TIMESTAMP)


def start_lines(file: str, labels: str, total: int, count: int, printer: str, ts: str) -> list[str]:
    """The small text under START: the job, which labels of how many (a resume reads 'labels 97-128 of 128'),
    the count, the printer and the time."""
    return [Path(file).stem, f"labels {labels} of {total}", f"{count} labels · {printer} · {ts}"]


def end_lines(file: str, count: int, ts: str) -> list[str]:
    """The small text under END: the job, what was sent, and what a missing END label means."""
    return [
        Path(file).stem,
        f"{count}/{count} sent · {ts}",
        "missing END label = job was cut: count back and report the last label number",
    ]


def _font(name: str, px: int) -> ImageFont.FreeTypeFont:
    from PIL import ImageFont

    from .text import find_font

    path = find_font(name)
    if path is None:
        raise ValueError(f"bookends need the bundled face {name!r}")
    return ImageFont.truetype(path, max(1, px))


def _hatch(d, box: tuple[int, int, int, int], stripe: int) -> None:
    """White diagonal stripes across box=(x0, y0, x1, y1) — the hazard-tape look."""
    x0, y0, x1, y1 = box
    h = y1 - y0
    x = x0 - h
    while x < x1 + h:
        d.polygon([(x, y1), (x + stripe, y1), (x + stripe + h, y0), (x + h, y0)], fill=255)
        x += 2 * stripe


def _wrap(lines: Sequence[str], font, width: int) -> list[str]:
    """Each line broken at spaces so no piece is wider than ``width``; a word wider than that stays whole (the
    caller shrinks the font until it fits)."""
    out: list[str] = []
    for line in lines:
        words, cur = line.split(" "), ""
        for w in words:
            cand = f"{cur} {w}".strip()
            if cur and font.getlength(cand) > width:
                out.append(cur)
                cur = w
            else:
                cur = cand
        out.append(cur)
    return out


def _balance(lines: Sequence[str], font, width: int) -> list[str]:
    """Like :func:`_wrap`, but a line that must break is cut into the fewest pieces that fit, each break at the
    space nearest an even share of the words — no piece strands one short word on a line of its own."""
    out: list[str] = []
    for line in lines:
        words = line.split(" ")
        greedy = _wrap([line], font, width)
        k = len(greedy)
        if k == 1 or len(words) < k:
            out.extend(greedy)
            continue
        if " · " in line:  # a line of fields breaks between fields first, the separator dropped
            fields = line.split(" · ")
            if all(font.getlength(f) <= width for f in fields):
                out.extend(fields)
                continue
        while k <= len(words):
            pieces, i, ok = [], 0, True
            for j in range(1, k + 1):
                end = len(words) if j == k else round(j * len(words) / k)
                piece = " ".join(words[i:end])
                if font.getlength(piece) > width:
                    ok = False
                    break
                pieces.append(piece)
                i = end
            if ok:
                out.extend(pieces)
                break
            k += 1
        else:
            out.extend(greedy)
    return out


def _largest(face: str, text: str, max_w: float, max_h: float, px: int) -> ImageFont.FreeTypeFont:
    """The largest size of ``face`` from ``px`` down (never under 4 dots/em) at which ``text`` fits
    ``max_w`` × ``max_h`` dots — the 4-dot floor is returned as is, the caller's area is already tiny."""
    px = max(4, px)
    while px > 4:
        font = _font(face, px)
        bb = font.getbbox(text)
        if bb[2] - bb[0] <= max_w and bb[3] - bb[1] <= max_h:
            return font
        px -= 1
    return _font(face, 4)


def _fit_small(
    lines: Sequence[str], face: str, width: int, height: int, px: int, balance: bool = False
) -> tuple[list[str], ImageFont.FreeTypeFont]:
    """The small lines wrapped and the font shrunk until every line fits the width and the block fits the
    height — never clipped. With ``balance``, a line keeps to ONE line when the face that allows it is at
    least three quarters of the wrapped face, and a line that must break is split evenly (:func:`_balance`)."""
    best: tuple[list[str], ImageFont.FreeTypeFont] | None = None
    p = px
    while p >= 4:
        font = _font(face, p)
        wrapped = _balance(lines, font, width) if balance else _wrap(lines, font, width)
        lp = round(p * 1.15)
        if max(font.getlength(t) for t in wrapped) <= width and lp * len(wrapped) <= height:
            best = wrapped, font
            break
        p -= 1
    if best is None:
        raise ValueError(f"bookend text does not fit {width}×{height} dots even at 4 dots/em: {list(lines)}")
    if balance and len(best[0]) > len(lines):
        p = int(best[1].size)
        while p >= max(4, round(int(best[1].size) * 0.75)):
            font = _font(face, p)
            if max(font.getlength(t) for t in lines) <= width and round(p * 1.15) * len(lines) <= height:
                return list(lines), font
            p -= 1
    return best


def tag_rect(m: Media, k: int) -> tuple[int, int, int, int]:
    """Where print area ``k``'s bookend is drawn, (x, y, w, h) dots in the physical frame: the whole die-cut label
    when ``labels_in`` names one that contains the area and is larger than it, else the print area itself."""
    ax, ay, aw, ah = m.area_px(k)
    if k < len(m.labels_in):
        lx, ly, lw, lh = (m.dots(v) for v in m.labels_in[k])
        if lw * lh > aw * ah and lx <= ax and ly <= ay and lx + lw >= ax + aw and ly + lh >= ay + ah:
            return lx, ly, lw, lh
    return ax, ay, aw, ah


def _printer_rect(m: Media, rect: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """A physical-frame rect in the printer frame, as :meth:`Media.printer_rect` does for a print area."""
    x, y, w, h = rect
    if m.rotate180:
        x, y = m.width_px - (x + w), m.length_px - (y + h)
    return x, y, w, h


def _scissors(d, cx: int, cy: int, size: int) -> None:
    """A scissors mark drawn with primitives (no face bundled here carries U+2702): two rings and two blades."""
    r = max(2, size // 5)
    lw = max(1, size // 10)
    d.ellipse([cx - size // 2, cy + r // 2, cx - size // 2 + 2 * r, cy + r // 2 + 2 * r], outline=0, width=lw)
    d.ellipse([cx - size // 2, cy - r // 2 - 2 * r, cx - size // 2 + 2 * r, cy - r // 2], outline=0, width=lw)
    d.line([cx - size // 2 + 2 * r, cy + r, cx + size // 2, cy - r], fill=0, width=lw)
    d.line([cx - size // 2 + 2 * r, cy - r, cx + size // 2, cy + r], fill=0, width=lw)


def _area_image(m: Media, area: int, word: str, lines: Sequence[str], cut: bool = False):
    """The print-on block of a bookend as a Pillow 'L' image of the area's size (physical frame, ink 0):
    black field, white border inset ~1 mm, hazard hatch along the top and bottom, ``word`` white in the largest
    bold face that fits, ``lines`` small and white under it (wrapped / shrunk, never clipped; none = the word
    takes the whole interior), and with ``cut`` a white "CUT HERE" band with the text in black."""
    from PIL import Image, ImageDraw

    _, _, w, h = m.area_px(area)
    inset, bw, hatch, stripe = (m.dots(v / 25.4) for v in (BORDER_INSET_MM, BORDER_MM, HATCH_MM, STRIPE_MM))
    hatch = max(hatch, 4)
    stripe = max(stripe, 2)
    x0, y0, x1, y1 = inset + bw + 2, hatch + 2, w - 1 - inset - bw - 2, h - 1 - hatch - 2
    iw, ih = x1 - x0, y1 - y0
    if iw < 20 or ih < 12:
        raise ValueError(f"print area {area} of {m.name} is too small for a bookend ({w}×{h} dots)")
    img = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(img)
    # hazard bands at the top and bottom edges, then the border over them
    _hatch(d, (0, 0, w, hatch), stripe)
    _hatch(d, (0, h - hatch, w, h), stripe)
    d.rectangle([inset, inset, w - 1 - inset, h - 1 - inset], outline=255, width=max(1, bw))
    # the interior: the word takes the upper part, the small lines the rest
    band = 0
    if cut:
        band = max(ih // 6, 8)
    word_h = (ih - band) * (0.42 if lines else 0.9)
    font = _largest("Inter Bold", word, iw * 0.92, word_h, int(word_h))
    bb = font.getbbox(word)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    wy = y0 + (word_h - th) / 2 - bb[1]
    d.text((x0 + (iw - tw) / 2 - bb[0], wy), word, fill=255, font=font)
    top_used = y0 + word_h
    if cut:  # the white band across the middle, between the word and the small lines
        by0 = int(top_used)
        d.rectangle([x0, by0, x1, by0 + band - 1], fill=255)
        bf = _largest("Inter Bold", "CUT HERE", iw * 0.6, band, int(band * 0.6))
        bbb = bf.getbbox("CUT HERE")
        bw_, bh_ = bbb[2] - bbb[0], bbb[3] - bbb[1]
        sz = max(6, int(band * 0.8))
        text_x = x0 + (iw - bw_) / 2 + sz * 0.6
        d.text((text_x - bbb[0], by0 + (band - bh_) / 2 - bbb[1]), "CUT HERE", fill=0, font=bf)
        _scissors(d, int(text_x - sz * 0.8), by0 + band // 2, sz)
        # a dashed cut line at both sides of the band
        dash = max(2, sz // 3)
        for xx in range(x0, int(text_x - sz * 1.6), 2 * dash):
            d.line(
                [xx, by0 + band // 2, min(xx + dash, x1), by0 + band // 2], fill=0, width=max(1, dash // 3)
            )
        for xx in range(int(text_x + bw_ + sz * 0.3), x1, 2 * dash):
            d.line(
                [xx, by0 + band // 2, min(xx + dash, x1), by0 + band // 2], fill=0, width=max(1, dash // 3)
            )
        top_used = by0 + band + 2
    if not lines:
        return img
    small_h = int(y1 - top_used)
    wrapped, sf = _fit_small(lines, "Inter", iw, small_h, max(5, int(small_h / (len(lines) * 1.3))))
    lp = round(sf.size * 1.15)
    y = top_used + (small_h - lp * len(wrapped)) / 2
    for t in wrapped:
        d.text((x0 + (iw - sf.getlength(t)) / 2, y), t, fill=255, font=sf)
        y += lp
    return img


def _tail(img, box: tuple[int, int, int, int], m: Media, lines: Sequence[str]) -> None:
    """The details in black on the clear tail ``box`` = (x0, y0, w, h) of a full-label mark: a hazard band at
    the label's far end, the lines centred in the largest face at which they all fit (wrapped, never clipped)."""
    from PIL import ImageDraw

    d = ImageDraw.Draw(img)
    x0, y0, w, h = box
    inset, hatch, stripe = (m.dots(v / 25.4) for v in (BORDER_INSET_MM, HATCH_MM, STRIPE_MM))
    hatch, stripe = max(hatch, 4), max(stripe, 2)
    d.rectangle([x0, y0 + h - hatch, x0 + w - 1, y0 + h - 1], fill=0)
    _hatch(d, (x0, y0 + h - hatch, x0 + w, y0 + h), stripe)
    tx, ty = x0 + inset + 2, y0 + inset + 2
    tw, th = w - 2 * (inset + 2), h - hatch - 2 * (inset + 2)
    if tw < 20 or th < 12:
        return
    wrapped, f = _fit_small(lines, "Inter Bold", tw, th, max(6, int(th / (len(lines) * 1.4))), balance=True)
    lp = round(f.size * 1.15)
    y = ty + (th - lp * len(wrapped)) / 2
    for t in wrapped:
        d.text((tx + (tw - f.getlength(t)) / 2, y), t, fill=0, font=f)
        y += lp


def bookend_image(m: Media, area: int, word: str, lines: Sequence[str], cut: bool = False):
    """One bookend for print area ``area`` as a Pillow 'L' image (physical frame, ink 0) of :func:`tag_rect`'s
    size. On a media whose die-cut label is larger than the area: the print-on block carries the hazard field
    and the word (and the CUT HERE band), the clear tail the ``lines`` in black (:func:`_tail`); otherwise the
    block carries everything (:func:`_area_image`), as before 0.18.0."""
    from PIL import Image

    lx, ly, lw, lh = tag_rect(m, area)
    ax, ay, aw, ah = m.area_px(area)
    if (lx, ly, lw, lh) == (ax, ay, aw, ah):
        return _area_image(m, area, word, lines, cut)
    img = Image.new("L", (lw, lh), 255)
    img.paste(_area_image(m, area, word, [], cut), (ax - lx, ay - ly))
    below, above = ly + lh - (ay + ah), ay - ly  # the tail is the larger remainder of the label
    if below >= above:
        _tail(img, (0, ay + ah - ly, lw, below), m, lines)
    else:
        _tail(img, (0, 0, lw, above), m, lines)
    return img


def _block(setup: bytes, m: Media, images: Sequence) -> bytes:
    """A label block with the job's own setup bytes (not a fresh ``setup_commands``): image k on print area k's
    :func:`tag_rect`, rotated into the printer frame as ``label_block`` does."""
    body = bytearray(setup)
    for k, img in enumerate(images):
        if m.rotate180:
            img = img.rotate(180)
        ax, ay, _, _ = _printer_rect(m, tag_rect(m, k))
        body += pattern_blocks(img, ax, ay)
    body += b"E" + EOL
    return bytes(body)


def bookend_blocks(
    m: Media, data: bytes, file: str, labels: str, total: int, count: int, printer: str, ts: str | None = None
) -> tuple[bytes, bytes]:
    """(START block, END block) for a job ``data`` (as it will be sent, after any selection) on media ``m``:
    one block each, every print area of the block carrying the mark, built on the job's own setup bytes.
    ``labels`` / ``total`` / ``count`` are the selection, the file's label count and the selected count;
    ``ts`` defaults to now."""
    blocks: list[LabelBlock] = parse_blocks(data)
    if not blocks:
        raise ValueError("the job has no label blocks to bookend")
    setup = blocks[0][0]
    ts = ts or now()
    per = m.labels_per_block
    start = _block(
        setup,
        m,
        [
            bookend_image(m, k, "START", start_lines(file, labels, total, count, printer, ts))
            for k in range(per)
        ],
    )
    end = _block(
        setup, m, [bookend_image(m, k, "END", end_lines(file, count, ts), cut=True) for k in range(per)]
    )
    return start, end


def with_bookends(
    m: Media, data: bytes, file: str, labels: str, total: int, count: int, printer: str, ts: str | None = None
) -> bytes:
    """The job stream with its bookends: START, the labels as given, END — one write."""
    start, end = bookend_blocks(m, data, file, labels, total, count, printer, ts)
    return start + data + end
