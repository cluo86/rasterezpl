"""Compose label jobs from typed text, pasted rows or an Easy-Mark project — the front end's own input, beyond
job files something else generated. A `layout` (rasterezpl.layouts) draws a decoration around the text: framed,
banner, sidebar, corners, ticket — or plain; the picture layouts take an image, the QR layouts (badge, qr-left,
qr) encode `qr`, a template filled per label like the text ({n}, or {column} with rows input).

A job is composed from a SPEC: the media, the face and size, alignment, copies, and the labels themselves as
lines of text. ``compose`` renders it with the same ``render_text`` the CLI uses and writes a ``.ezpl`` under
``<root>/composed/`` next to a ``.json`` sidecar holding the spec (what made the file, reloadable into the form),
so the result is an ordinary job file: it prints through the same plan / print step, guards and log as any other.

Text input: one label per block, blocks separated by a blank line, lines within a block are the label's lines;
``{n}`` in any line is the running number (``start`` and up); ``copies`` repeats each label.

Rows input: a header line and TSV/CSV rows, with a template whose ``{column}`` fields are filled per row.

Easy-Mark input: a ``.pemx`` project (a zip with ``project.json``) — every series' data are the label texts,
the series style carries the point size; the media is chosen in the form (the project names the stock).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import time
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .layouts import render_label
from .media import Media
from .registry import Registry
from .stream import count_labels, decode_block, job, parse_blocks
from .text import FONT_CANDIDATES, bundled_fonts, find_font, pt_to_px

COMPOSED_DIR = "composed"
FONTS_DIR = "fonts"


@dataclass
class Spec:
    media: str
    font: str
    pt: float
    labels: list[str] = field(default_factory=list)  # each label = its lines joined by "\n"
    name: str = "labels"
    align: str = "center"
    fit: bool = False
    copies: int = 1
    start: int = 1
    rotate180: bool = False
    area: int | None = None  # a single print area on multi-up media (default: fill areas in order)
    layout: str = "plain"  # a decoration around the text (rasterezpl.layouts)
    image: str | None = (
        None  # a picture for the logo / logo-top / image layouts: base64 (or data: URL) of a PNG, JPEG or SVG
    )
    image_mode: str = "dither"  # dither | threshold (rasterezpl.images)
    qr: str | None = None  # what the QR layouts encode: a template — {n} (and {column} with rows input)
    qrs: list[str] = field(
        default_factory=list
    )  # the per-label QR data, parallel to `labels` (filled by from_dict)

    @classmethod
    def from_dict(cls, d: dict) -> Spec:
        labels = d.get("labels")
        qr_tpl = str(d.get("qr") or "") or None
        qrs: list[str] = [str(x) for x in (d.get("qrs") or [])]
        if labels is None and d.get("text") is not None:
            labels = parse_text(str(d["text"]))
            if qr_tpl and not qrs:
                qrs = [qr_tpl] * len(labels)  # {n} is filled at expand time
        if labels is None and d.get("rows") is not None:
            labels = parse_rows(str(d["rows"]), str(d.get("template", "")))
            if qr_tpl and not qrs:
                qrs = parse_rows(str(d["rows"]), qr_tpl)
        if labels is None and qr_tpl and not qrs:  # a QR alone, no text
            labels, qrs = [""], [qr_tpl]
        return cls(
            media=str(d.get("media", "")),
            font=str(d.get("font", "")),
            pt=float(d.get("pt", 0) or 0),
            labels=[str(x) for x in (labels or [])],
            name=str(d.get("name") or "labels"),
            align=str(d.get("align", "center")),
            fit=bool(d.get("fit", False)),
            copies=max(1, int(d.get("copies", 1) or 1)),
            start=int(d.get("start", 1) or 1),
            rotate180=bool(d.get("rotate180", False)),
            area=None if d.get("area") in (None, "") else int(d["area"]),
            layout=str(d.get("layout") or "plain"),
            image=(str(d["image"]) if d.get("image") else None),
            image_mode=str(d.get("image_mode") or "dither"),
            qr=qr_tpl,
            qrs=qrs,
        )


def parse_text(text: str) -> list[str]:
    """Blocks separated by blank lines → labels; lines keep their order, trailing spaces dropped."""
    out = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip("\n")):
        lines = [ln.rstrip() for ln in block.split("\n")]
        while lines and not lines[-1]:
            lines.pop()
        if any(lines):
            out.append("\n".join(lines))
    return out


def parse_rows(rows: str, template: str) -> list[str]:
    """A header line + rows (tab or comma separated) → one label per row from `template`, whose {column}
    fields are filled from that row (``\\n`` in the template = a new line)."""
    text = rows.replace("\r\n", "\n").strip("\n")
    if not text:
        return []
    dialect = "excel-tab" if "\t" in text.split("\n", 1)[0] else "excel"
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    tpl = template.replace("\\n", "\n")
    fields = re.findall(r"\{([^}:]+)(?::[^}]*)?\}", tpl)
    header = reader.fieldnames or []
    missing = [f for f in fields if f not in header and f != "n"]
    if missing:
        raise ValueError(
            f"template fields not in the header: {', '.join(missing)} (columns: {', '.join(header)})"
        )
    out = []
    for i, row in enumerate(reader, 1):
        vals = {k: (v or "").strip() for k, v in row.items() if k is not None}
        out.append(tpl.format(**vals, n=i) if fields else "\n".join(vals.values()))
    return out


def expand(labels: list[str], copies: int = 1, start: int = 1) -> list[str]:
    """{n} → the running number per label, then `copies` of each (also used for the per-label QR data)."""
    out = []
    for i, lab in enumerate(labels):
        text = lab.replace("{n}", str(start + i))
        out.extend([text] * max(1, copies))
    return out


def fonts_available(root: Path | None = None) -> list[dict]:
    """Faces this machine can print: the known candidates that resolve, plus any .ttf/.otf under <root>/fonts."""
    out = [{"name": n, "path": p, "bundled": True} for n, p in bundled_fonts().items()]
    for name in FONT_CANDIDATES:
        found = find_font(name)
        if found:
            out.append({"name": name, "path": found})
    if root is not None and (root / FONTS_DIR).is_dir():
        for f in sorted((root / FONTS_DIR).glob("*")):
            if f.suffix.lower() in (".ttf", ".otf"):
                out.append({"name": f.stem, "path": str(f)})
    return out


def _slug(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-.")
    return s or "labels"


def render(reg: Registry, spec: Spec, root: Path | None = None) -> tuple[bytes, Media, str, list[str]]:
    """The job bytes for a spec (validated: media on record, face present, at least one label)."""
    if spec.media not in reg.media:
        raise ValueError(f"unknown media {spec.media!r}")
    m = reg.media[spec.media]
    font = find_font(spec.font) if spec.font else None
    if font is None and root is not None and spec.font:
        cand = root / FONTS_DIR / spec.font
        font = str(cand) if cand.exists() else None
    if font is None:
        raise ValueError(
            f"font {spec.font!r} is not on this machine (no substitute: a different face prints a different label)"
        )
    if not spec.pt or spec.pt <= 0:
        raise ValueError("pt must be a positive size")
    texts = expand(spec.labels or ([""] if spec.layout in ("image", "qr") else []), spec.copies, spec.start)
    if not texts:
        raise ValueError("no label text")
    qrs = expand(spec.qrs or ([spec.qr] * len(spec.labels) if spec.qr else []), spec.copies, spec.start)
    if qrs and len(qrs) != len(texts):
        raise ValueError(f"{len(qrs)} QR data for {len(texts)} labels")
    per = m.labels_per_block
    if spec.area is not None and not 0 <= spec.area < per:
        raise ValueError(f"area {spec.area} is not one of this media's {per} print area(s)")
    px = pt_to_px(spec.pt, m.dpi)
    picture = None
    if spec.image:
        from .images import MODES, load_image

        if spec.image_mode not in MODES:
            raise ValueError(f"image_mode {spec.image_mode!r}: one of {', '.join(MODES)}")
        picture = load_image(spec.image, width_px=max(m.area_px(spec.area or 0)[2] * 2, 600))
    imgs = []
    for i, t in enumerate(texts):
        k = spec.area or 0
        _, _, aw, ah = m.area_px(k)
        im = render_label(
            aw,
            ah,
            t.split("\n"),
            font,
            px,
            spec.layout,
            spec.align,
            spec.fit,
            image=picture,
            qr=qrs[i] if qrs else None,
        )
        if spec.rotate180:
            im = im.rotate(180)
        imgs.append(im)
    rows: list[list] = []
    if spec.area is not None:
        for im in imgs:
            row: list = [None] * per
            row[spec.area] = im
            rows.append(row)
    else:
        rows = [imgs[i : i + per] for i in range(0, len(imgs), per)]
    return job(m, rows), m, font, texts


def compose(root: Path, reg: Registry, spec: Spec) -> dict:
    """Render and save: <root>/composed/<name>-<hash>.ezpl + .json sidecar (the spec, the font path, when)."""
    data, m, font, texts = render(reg, spec, root)
    digest = hashlib.sha256(data).hexdigest()[:8]
    d = root / COMPOSED_DIR
    d.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(spec.name)}-{digest}"
    out = d / f"{stem}.ezpl"
    out.write_bytes(data)
    side = {
        **asdict(spec),
        "image": (f"<{len(spec.image)} chars of image data>" if spec.image else None),
        "qr_data": expand(spec.qrs, spec.copies, spec.start) if spec.qrs else None,
        "font_path": font,
        "texts": texts,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "sha256_8": digest,
    }
    (d / f"{stem}.json").write_text(json.dumps(side, ensure_ascii=False, indent=1), encoding="utf-8")
    return {
        "file": f"{COMPOSED_DIR}/{stem}.ezpl",
        "spec": f"{COMPOSED_DIR}/{stem}.json",
        "labels": len(texts),
        "blocks": count_labels(data),
        "media": m.name,
        "font_path": font,
    }


def preview_png(reg: Registry, spec: Spec, root: Path | None = None, block: int = 1) -> bytes:
    """The proof of one web row of the spec, rendered in memory (nothing saved)."""
    from .proof import proof_image

    data, m, _, texts = render(reg, spec, root)
    blocks = parse_blocks(data)
    bn = min(max(1, block), len(blocks))
    img = proof_image(
        m, decode_block(blocks[bn - 1][1], m), caption=f"{spec.name} — row {bn} of {len(blocks)} ({m.name})"
    )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def read_spec(root: Path, rel: str) -> dict:
    p = (root / rel.lstrip("/")).resolve()
    if root.resolve() not in p.parents or p.suffix != ".json":
        raise ValueError(f"{rel!r} is not a composed spec under the root")
    return json.loads(p.read_text(encoding="utf-8"))


def import_pemx(data: bytes) -> dict:
    """An Easy-Mark Plus project → its label texts (every series' data, in order), the point size of the first
    series' style, and the part the format names."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "project.json" not in z.namelist():
                raise ValueError("not an Easy-Mark project: no project.json in the zip")
            doc = json.loads(z.read("project.json").decode("utf-8"))
    except zipfile.BadZipFile as e:
        raise ValueError("not an Easy-Mark project: not a zip file") from e
    texts: list[str] = []
    pt = None
    part = None
    for d in doc.get("documents", []):
        fmt = d.get("format") or {}
        part = part or fmt.get("partName")
        for s in d.get("series", []):
            if pt is None:
                pt = (s.get("style") or {}).get("fontSize")
            texts.extend(str(t) for t in (s.get("data") or []))
    if not texts:
        raise ValueError("the project carries no series data (no label texts)")
    return {"texts": texts, "pt": pt, "part": part, "labels": len(texts)}
