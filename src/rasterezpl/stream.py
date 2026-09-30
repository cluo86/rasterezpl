"""Bitmaps to EZPL pattern blocks and back: encode a job, read our own stream, select labels, decode a page."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .media import EOL, Media, setup_commands

STRIP_ROWS = 8  # rows per Q block, as rastertoezpl's GDX_BUFFER_HEIGHT

QBlock = tuple[int, int, int, int, bytes]  # x, y, width_bytes, rows, raw
LabelBlock = tuple[bytes, list[QBlock]]  # setup bytes up to and including ^L\r, its Q blocks


def bitmap_rows(img) -> tuple[int, int, list[bytes]]:
    """A PIL image → (width_bytes, height, rows): 1 bits = black dots, MSB first, each row padded to bytes."""
    from PIL import Image, ImageOps

    g = img.convert("L")
    bw = ImageOps.invert(g).convert("1", dither=Image.Dither.NONE)  # black ink → 1
    w, h = bw.size
    stride = (w + 7) // 8
    raw = bw.tobytes()
    if len(raw) != stride * h:
        raise ValueError(f"unexpected packed size {len(raw)} for {w}×{h}")
    return stride, h, [raw[i * stride : (i + 1) * stride] for i in range(h)]


def pattern_blocks(img, x: int, y: int) -> bytes:
    """The image as Q blocks of STRIP_ROWS rows at (x, y) dots; all-white strips are skipped.

    Q takes x in dots but the data is bytes, so the image is padded on the left to the byte boundary below x —
    the content stays at exactly x.
    """
    pad = x % 8
    if pad:
        from PIL import Image

        wide = Image.new("L", (img.size[0] + pad, img.size[1]), 255)
        wide.paste(img.convert("L"), (pad, 0))
        img, x = wide, x - pad
    stride, h, rows = bitmap_rows(img)
    out = bytearray()
    for top in range(0, h, STRIP_ROWS):
        strip = rows[top : top + STRIP_ROWS]
        if not any(any(b) for b in strip):
            continue
        out += f"Q{x},{y + top},{stride},{len(strip)}".encode() + EOL
        out += b"".join(strip) + EOL
    return bytes(out)


def label_block(m: Media, images: Sequence) -> bytes:
    """One printed label (one ^L … E): image k drawn in print area k (None = that area stays blank)."""
    if len(images) > len(m.areas_in):
        raise ValueError(f"{len(images)} images for {len(m.areas_in)} print areas")
    body = bytearray(setup_commands(m) + b"^L" + EOL)
    for k, img in enumerate(images):
        if img is None:
            continue
        ax, ay, aw, ah = m.area_px(k)
        if img.size != (aw, ah):
            raise ValueError(f"image {img.size} is not the print area {(aw, ah)} of {m.name} area {k}")
        if m.rotate180:
            img = img.rotate(180)
        ax, ay, _, _ = m.printer_rect(k)
        if ax < 0 or ay < 0:
            raise ValueError(f"print area {k} of {m.name} lies outside the label ({ax}, {ay})")
        body += pattern_blocks(img, ax, ay)
    body += b"E" + EOL
    return bytes(body)


def job(m: Media, labels: Sequence[Sequence]) -> bytes:
    """A whole job: one label block per entry (each entry = the images for that label's print areas)."""
    return b"".join(label_block(m, imgs) for imgs in labels)


# --- reading the stream back. A job is `<setup>^L\r(Q<x>,<y>,<bytes>,<rows>\r<data>\r)*E\r` repeated; the data is
# binary (it can contain \r and E), so blocks are walked by their declared lengths, never split on bytes.
def parse_blocks(data: bytes) -> list[LabelBlock]:
    out: list[LabelBlock] = []
    i, n = 0, len(data)
    while i < n:
        j = data.find(b"^L" + EOL, i)
        if j < 0:
            break
        setup = data[i : j + 3]
        i = j + 3
        qs: list[QBlock] = []
        while True:
            if data.startswith(b"E" + EOL, i):
                i += 2
                break
            if not data.startswith(b"Q", i):
                raise ValueError(f"unexpected bytes at {i}: {data[i : i + 12]!r}")
            k = data.index(EOL, i)
            x, y, wb, rows = (int(v) for v in data[i + 1 : k].split(b","))
            raw = data[k + 1 : k + 1 + wb * rows]
            if len(raw) != wb * rows or data[k + 1 + wb * rows : k + 2 + wb * rows] != EOL:
                raise ValueError(f"truncated Q block at {i}")
            qs.append((x, y, wb, rows, raw))
            i = k + 2 + wb * rows
        out.append((setup, qs))
    return out


def serialise_block(setup: bytes, qs: Iterable[QBlock]) -> bytes:
    out = bytearray(setup)
    for x, y, wb, rows, raw in qs:
        out += f"Q{x},{y},{wb},{rows}".encode() + EOL + raw + EOL
    out += b"E" + EOL
    return bytes(out)


def count_labels(data: bytes) -> int:
    return len(parse_blocks(data))


def first_labels(data: bytes, n: int) -> bytes:
    """The first n label blocks of a job (a test print)."""
    return select_blocks(data, range(1, n + 1))


def parse_selection(spec: str, total: int) -> list[int]:
    """A print-dialog range — "1", "1-2", "1,3,5-6" — to sorted 1-based indices within 1 … total."""
    picked: set[int] = set()
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        lo, _, hi = part.partition("-")
        a, b = int(lo), int(hi or lo)
        if a < 1 or b > total or a > b:
            raise ValueError(f"selection {part!r} outside 1–{total}")
        picked.update(range(a, b + 1))
    return sorted(picked)


def select_blocks(data: bytes, indices: Iterable[int]) -> bytes:
    """The blocks (1-based) of a job, re-serialised byte for byte."""
    want = set(indices)
    return b"".join(
        serialise_block(setup, qs) for k, (setup, qs) in enumerate(parse_blocks(data), start=1) if k in want
    )


def decode_block(qs: Iterable[QBlock], m: Media):
    """A block's Q data painted back into a page bitmap of the media (printer frame; ink 0, paper 255)."""
    from PIL import Image

    page = Image.new("L", (m.width_px, m.length_px), 255)
    for x, y, wb, rows, raw in qs:
        mask = Image.frombytes("1", (wb * 8, rows), raw).convert("L")  # set bit (ink) → 255
        page.paste(0, (x, y), mask)
    return page


def decode_job(data: bytes, m: Media) -> list:
    return [decode_block(qs, m) for _setup, qs in parse_blocks(data)]


def select_labels(data: bytes, spec: str, m: Media) -> bytes:
    """A print-dialog selection at LABEL granularity: label k lives in block (k-1)//per, area (k-1)%per, where
    per = labels per block. A block with only some of its areas selected is decoded, the other areas blanked and
    re-encoded; a fully selected block passes through byte for byte."""
    blocks = parse_blocks(data)
    per = m.labels_per_block
    want = parse_selection(spec, len(blocks) * per)
    by_block: dict[int, set[int]] = {}
    for k in want:
        by_block.setdefault((k - 1) // per, set()).add((k - 1) % per)
    out = bytearray()
    for bi, (setup, qs) in enumerate(blocks):
        areas = by_block.get(bi)
        if not areas:
            continue
        if len(areas) == per:
            out += serialise_block(setup, qs)
            continue
        page = decode_block(qs, m)
        out += setup
        for a in sorted(areas):
            ax, ay, aw, ah = m.printer_rect(a)
            out += pattern_blocks(page.crop((ax, ay, ax + aw, ay + ah)), ax, ay)  # each kept area at its rect
        out += b"E" + EOL
    return bytes(out)


def blocks_outside_areas(data: bytes, m: Media) -> list[tuple[int, int, int]]:
    """(block number, x, y) of every Q block that does not lie inside one of the media's print areas in the
    printer frame. A job written for another frame, pitch or dpi of the same stock fails this — e.g. a file
    rasterised before a geometry fix (2026-09-26: blocks at y 83–171 in a leading-edge frame printed on the
    laminate; the media's areas begin at y 431). Empty = every block is where the media says it prints."""
    rects = [m.printer_rect(k) for k in range(len(m.areas_in))]
    bad = []
    for n, (_setup, qs) in enumerate(parse_blocks(data), start=1):
        for x, y, wb, rows, _raw in qs:
            x1, y1 = x + wb * 8, y + rows
            if not any(
                ax - 7 <= x and x1 <= ax + aw + 7 and ay <= y and y1 <= ay + ah + 7
                for ax, ay, aw, ah in rects
            ):
                bad.append((n, x, y))
    return bad


def header_of(data: bytes) -> tuple[int, int, int] | None:
    """(length_mm, gap_mm, width_mm) from a job's first ^Q/^W header, or None if it is not our stream."""
    import re

    # a spooled job may carry the printer's registration in front (^R<x>\r ~Q<±y>\r, offset_commands) — skip it
    m = re.match(rb"(?:\^R\d+\r|~Q[+-]?\d+\r)*\^Q(\d+),(\d+)\r\^W(\d+)\r", data)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def matching_media(data: bytes, medias: dict[str, Media]) -> list[str]:
    """Names of the media a job could have been written for: same ^Q/^W header (the header carries mm, not dpi,
    so the same stock at two resolutions matches twice) and every Q block inside the page."""
    h = header_of(data)
    if h is None:
        raise ValueError("not an EZPL job written by rasterezpl (no ^Q/^W header)")
    blocks = parse_blocks(data)
    xmax = max((x + wb * 8 for _s, qs in blocks for x, _y, wb, _r, _raw in qs), default=0)
    ymax = max((y + rows for _s, qs in blocks for _x, y, _wb, rows, _raw in qs), default=0)
    return [
        n
        for n, m in medias.items()
        if (round(m.length_mm), round(m.gap_mm), round(m.width_mm)) == h
        and xmax <= m.width_px + 7
        and ymax <= m.length_px
    ]


def match_media(data: bytes, medias: dict[str, Media]) -> tuple[str, Media]:
    """The one media a job was written for (see matching_media); ambiguity is an error — name the media."""
    hits = matching_media(data, medias)
    if len(hits) != 1:
        h = header_of(data) or (0, 0, 0)
        raise ValueError(f"header ^Q{h[0]},{h[1]} ^W{h[2]} matches {len(hits)} media: {hits}")
    return hits[0], medias[hits[0]]
