"""A label project someone designed elsewhere — GoLabel (`.ezpx`) or Easy-Mark Plus (`.pemx`) — rendered by THIS
engine into an ordinary job: the site's existing design prints through rasterezpl's guards, log and one-rendering
promise, and a proof shows what our engine makes of their layout.

What this is not: a GoLabel or Easy-Mark emulator. Both programs lay text out with the Windows font engine and
their own wrapping; we lay it out with FreeType's basic engine (the same on every machine) and our own wrapping.
Every result carries the sentence :data:`DISCLAIMER` — "our rendering of this layout — GoLabel/EasyMark render
with their own engine; wraps and clipping may differ" — and nothing a file asks for is dropped silently: a face
not on this machine is a refusal unless a substitute is named (and then the summary says which); a box whose
rectangle leaves the label (GoLabel prints nothing for such a box, 2026-11-06) is a refusal unless ``partial``
says to leave it out, as GoLabel does; a shape kind this importer does not draw is the same refusal.

`.ezpx` (GoLabel, plain XML): ``Setup`` carries the label (``LabelWidth`` × ``LabelLength`` mm, ``GapLength``,
``Dpi``, speed, darkness, stop, ``Copy``, the ``CopyField``), ``Layout`` the labels across; each text box is a
``GraphicShape`` of ``xsi:type="WindowTextBox"``: ``FontCmd="<face>,<pt>…"``, a ``BoundRect`` in dots of the
design frame (top-left origin, the physical frame of the label — the frame of every design program's page),
``Direction`` ``Angle0`` or ``Angle180`` (the text turned round inside its rectangle: the back half of a
wrap-around flag), ``Alignment``, ``LineHeight``, ``Data`` = the text with ``^F0n`` fields and ``DispData`` = the
preview text saved in the file. **``^F0n`` is the ZERO-BASED column n of the data source** — GoLabel binds a
field to a column's position, never its header (learned by misprint: ``^F6`` printed the seventh column). The
workbook the file points at (``DataBaseFilePath``, a Windows path on the designer's laptop) is never opened:
the data comes with ``--data``, CSV or ``.xlsx``. ``CopyField`` is read (copies per row when ``bCopyDataBase``
is set) and never rewritten.

`.pemx` (Easy-Mark Plus): a zip of ``project.json``; one document per stock (``format.partName``, the page and
the print-on label in inches), the label texts in ``series[].data`` with the series' face and point size,
``wireMode`` = a short label's text repeated down the print-on area, as Easy-Mark does.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .compose import sniff_delimiter
from .media import Media
from .registry import Registry
from .stream import count_labels, job
from .text import find_font, load_font, pt_to_px

DISCLAIMER = "our rendering of this layout — GoLabel/EasyMark render with their own engine; wraps and clipping may differ"
XSI = "{http://www.w3.org/2001/XMLSchema-instance}"
FIELD = re.compile(r"\^F(\d+)")


# -- the designs ---------------------------------------------------------------------------------------------------
@dataclass
class Box:
    """One GoLabel text box, in the design frame (dots at the design's dpi, top-left origin)."""

    n: int  # 1-based, file order
    data: str  # the Data text: literal text and ^F0n fields, lines separated by \n
    disp: str  # DispData: the preview text saved in the file
    rect: tuple[float, float, float, float]  # x, y, w, h
    face: str
    pt: float
    line_height: float | None  # dots per line (the file's LineHeight), None = 1.2 em
    align: str = "center"  # left | center | right
    rotated: bool = False  # Direction Angle180: the text turned round inside its rectangle
    wrap: bool = True  # WrapMode WordWrap
    printed: bool = True  # IsPrint

    def fields(self) -> list[int]:
        return sorted({int(m) for m in FIELD.findall(self.data)})

    def label(self) -> str:
        return (
            f"box {self.n} ({self.data!r}" + (f", preview {self.disp!r}" if self.disp.strip() else "") + ")"
        )


@dataclass
class EzpxDesign:
    label_mm: tuple[float, float]  # width across the head, length along the feed
    gap_mm: float
    dpi: int
    across: int = 1  # Layout HorAcross: labels across the web
    boxes: list[Box] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # shapes this importer does not draw
    copies: int = 1  # Setup Copy
    copy_field: int | None = None  # CopyField ^F0n → the column that holds the copies per row …
    copies_from_data: bool = False  # … when bCopyDataBase is set
    header: bool = True  # the data source has a header row (not DatabaseNoHeader)
    sheet: str | None = None  # from DataBaseSelection
    db_path: str = ""  # DataBaseFilePath — reported, never opened
    db_format: str = ""
    row_index: int | None = None  # the file's RowIndex (rows − 1 when it was saved)
    speed: int | None = None
    darkness: int | None = None
    stop_mm: int | None = None
    margins_mm: tuple[float, float] = (0.0, 0.0)  # LeftMargin, TopMargin — reported, not applied
    printer: str = ""

    @property
    def width_px(self) -> int:
        return round(self.label_mm[0] / 25.4 * self.dpi)

    @property
    def length_px(self) -> int:
        return round(self.label_mm[1] / 25.4 * self.dpi)

    def describe(self) -> str:
        w, ln = self.label_mm
        return (
            f"GoLabel design: label {w:g} × {ln:g} mm, gap {self.gap_mm:g} mm, {self.dpi} dpi, "
            f"{len(self.boxes)} text box{'es' if len(self.boxes) != 1 else ''}, {self.across} across"
            + (f", for a {self.printer}" if self.printer else "")
        )


@dataclass
class PemxDesign:
    part: str
    texts: list[str]
    pt: float
    face: str
    label_in: tuple[float, float]  # the print-on label, inches
    page_in: tuple[float, float]  # the page (web row), inches
    columns: int
    wire_mode: bool
    align: str = "center"
    version: str = ""

    def describe(self) -> str:
        return (
            f"Easy-Mark project: {self.part}, {len(self.texts)} labels, {self.face} {self.pt:g} pt, label "
            f"{self.label_in[0]:g} × {self.label_in[1]:g} in on a {self.page_in[0]:g} × {self.page_in[1]:g} in "
            f"page, {self.columns} across" + (", wire mode" if self.wire_mode else "")
        )


@dataclass
class Imported:
    """A job rendered from a design: the bytes, the media they are for, the count, and the notes an operator
    must read (what was matched, substituted, wrapped, left out — the disclaimer last)."""

    data: bytes
    media: Media
    labels: int
    notes: list[str]
    source: str
    media_name: str = ""  # the registry key of the media (``media.name`` is its display name)

    @property
    def blocks(self) -> int:
        return count_labels(self.data)


# -- reading .ezpx -------------------------------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(e: ET.Element | None, default: str = "") -> str:
    return (e.text or default) if e is not None else default


def _float(v: str | None, default: float = 0.0) -> float:
    try:
        return float(v) if v not in (None, "") else default
    except ValueError:
        return default


def _int(v: str | None) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except ValueError:
        return None


def _mm(v: float, unit: str, dpi: int) -> float:
    u = unit.lower()
    if u.startswith("inch"):
        return v * 25.4
    if u.startswith("dot"):
        return v / dpi * 25.4
    return v


def read_ezpx(data: bytes) -> EzpxDesign:
    """Parse a GoLabel ``.ezpx`` (see the module docstring for the elements read)."""
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise ValueError(f"not a GoLabel .ezpx: {e}") from e
    if _local(root.tag) != "PrintJob":
        raise ValueError(f"not a GoLabel .ezpx: the root element is <{_local(root.tag)}>, not <PrintJob>")
    setup = next((e for e in root.iter() if _local(e.tag) == "Setup"), None)
    if setup is None:
        raise ValueError("not a GoLabel .ezpx: no <Setup> (label size, dpi)")
    a = setup.attrib
    dpi = _int(_text(setup.find("Dpi"))) or 203
    unit = _text(setup.find("UnitType"), "Mm")
    w = _mm(_float(a.get("LabelWidth")), unit, dpi)
    ln = _mm(_float(a.get("LabelLength")), unit, dpi)
    if w <= 0 or ln <= 0:
        raise ValueError(
            f"the design has no label size (LabelWidth {a.get('LabelWidth')!r}, LabelLength {a.get('LabelLength')!r})"
        )
    layout = setup.find("Layout")
    copy_field = None
    m = FIELD.search(a.get("CopyField", "") or "")
    if m:
        copy_field = int(m.group(1))
    label = next((e for e in root.iter() if _local(e.tag) == "Label"), None)
    sheet = None
    sel = _text(label.find("DataBaseSelection")) if label is not None else ""
    ms = re.search(r"`'?([^`'$]+)\$?'?`", sel)
    if ms:
        sheet = ms.group(1)
    design = EzpxDesign(
        label_mm=(w, ln),
        gap_mm=_mm(_float(a.get("GapLength")), unit, dpi),
        dpi=dpi,
        across=max(1, _int(layout.attrib.get("HorAcross")) or 1) if layout is not None else 1,
        copies=max(1, _int(a.get("Copy")) or 1),
        copy_field=copy_field,
        copies_from_data=(a.get("bCopyDataBase", "false").lower() == "true"),
        header=(_text(label.find("DatabaseNoHeader"), "false").strip().lower() != "true")
        if label is not None
        else True,
        sheet=sheet,
        db_path=_text(label.find("DataBaseFilePath")).strip() if label is not None else "",
        db_format=_text(label.find("DataBaseFormat")).strip() if label is not None else "",
        row_index=_int(_text(label.find("RowIndex"))) if label is not None else None,
        speed=_int(a.get("Speed")),
        darkness=_int(a.get("Darkness")),
        stop_mm=_int(a.get("Stop")),
        margins_mm=(_mm(_float(a.get("LeftMargin")), unit, dpi), _mm(_float(a.get("TopMargin")), unit, dpi)),
        printer=_text(root.find("PrinterModel")).strip(),
    )
    n = 0
    for g in root.iter():
        if _local(g.tag) != "GraphicShape":
            continue
        n += 1
        kind = g.attrib.get(f"{XSI}type", g.attrib.get("type", "?"))
        if kind != "WindowTextBox":
            design.skipped.append(f"shape {n}: {kind}")
            continue
        ga = g.attrib
        rotated = ga.get("Direction", "Angle0") == "Angle180"
        br = g.find("BoundRect")
        if br is not None and br.find("Location") is not None and br.find("Size") is not None:
            rect = (
                _float(_text(br.find("Location/X"))),
                _float(_text(br.find("Location/Y"))),
                _float(_text(br.find("Size/Width"))),
                _float(_text(br.find("Size/Height"))),
            )
        else:  # the anchor is the top-left corner upright, the bottom-right corner turned round
            bw, bh = _float(_text(g.find("BoundRectWidth"))), _float(_text(g.find("BoundRectHeight")))
            x, y = _float(ga.get("X")), _float(ga.get("Y"))
            rect = (x - bw, y - bh, bw, bh) if rotated else (x, y, bw, bh)
        cmd = (ga.get("FontCmd") or "").replace("\r", "").replace("\n", "")
        parts = [p.strip() for p in cmd.split(",")]
        face = parts[0] if parts and parts[0] else "?"
        pt = _float(parts[1]) if len(parts) > 1 else 0.0
        if pt <= 0:
            pt = _float(ga.get("FontHeight"), 8.0) or 8.0
        align = {"left": "left", "right": "right"}.get(ga.get("Alignment", "Center").lower(), "center")
        design.boxes.append(
            Box(
                n=n,
                data=_text(g.find("Data")).replace("\r\n", "\n").replace("\r", "\n"),
                disp=_text(g.find("DispData")).replace("\r\n", "\n").replace("\r", "\n"),
                rect=rect,
                face=face,
                pt=pt,
                line_height=_float(ga.get("LineHeight")) or None,
                align=align,
                rotated=rotated,
                wrap=(_text(g.find("WrapMode"), "WordWrap").strip().lower() != "nowrap"),
                printed=(ga.get("IsPrint", "true").lower() != "false"),
            )
        )
    if not design.boxes and not design.skipped:
        raise ValueError("the design has no shapes")
    return design


# -- reading .pemx -------------------------------------------------------------------------------------------------
def read_pemx(data: bytes) -> PemxDesign:
    """Parse an Easy-Mark Plus ``.pemx`` (the first document's format and every series' texts)."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "project.json" not in z.namelist():
                raise ValueError("not an Easy-Mark project: no project.json in the zip")
            doc = json.loads(z.read("project.json").decode("utf-8"))
            version = (
                z.read("project-version.txt").decode("utf-8").strip()
                if "project-version.txt" in z.namelist()
                else ""
            )
    except zipfile.BadZipFile as e:
        raise ValueError("not an Easy-Mark project: not a zip file") from e
    docs = doc.get("documents") or []
    if not docs:
        raise ValueError("the project has no documents")
    d0 = docs[0]
    fmt = d0.get("format") or {}
    texts: list[str] = []
    pt: float | None = None
    face: str | None = None
    align = "center"
    for d in docs:
        for s in d.get("series", []):
            st = s.get("style") or {}
            if pt is None and st.get("fontSize"):
                pt = float(st["fontSize"])
            if face is None and st.get("fontFamily"):
                face = str(st["fontFamily"])
            texts.extend(str(t) for t in (s.get("data") or []))
        for lab in d.get("labels", []):
            for para in lab.get("inlineContent") or []:
                ta = (para.get("style") or {}).get("textAlign")
                if ta in ("left", "center", "right"):
                    align = ta
                    break
    if not texts:
        raise ValueError("the project carries no series data (no label texts)")
    deco_font = ((d0.get("currentDecoration") or fmt.get("decoration") or {}).get("font")) or {}
    label = ((fmt.get("dimensions") or {}).get("label")) or {}
    page = ((fmt.get("page") or {}).get("size")) or {}
    cols = ((fmt.get("page") or {}).get("layout") or {}).get("columns") or 1
    unit = str(fmt.get("unit") or "INCHES").upper()
    k = 1 / 25.4 if unit.startswith("MM") or unit.startswith("MILLI") else 1.0
    return PemxDesign(
        part=str(fmt.get("partName") or fmt.get("name") or d0.get("name") or "?"),
        texts=texts,
        pt=float(pt or deco_font.get("size") or 0.0),
        face=str(face or deco_font.get("name") or "Arial"),
        label_in=(float(label.get("width") or 0) * k, float(label.get("height") or 0) * k),
        page_in=(float(page.get("width") or 0) * k, float(page.get("height") or 0) * k),
        columns=int(cols),
        wire_mode=bool(d0.get("wireMode", fmt.get("wireMode", False))),
        align=align,
        version=version,
    )


# -- the data --------------------------------------------------------------------------------------------------------
def read_table(path: Path, sheet: str | None = None) -> tuple[list[list[str]], str | None]:
    """The rows of a ``.csv`` / ``.tsv`` / ``.txt`` (delimiter sniffed, BOM tolerated) or of one sheet of a
    ``.xlsx`` (openpyxl, the ``xlsx`` extra; ``sheet`` by name, default the first) as strings, trailing empty
    rows dropped; and the sheet used."""
    if path.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as e:
            raise RuntimeError(
                f"reading {path.name} needs openpyxl: pip install 'rasterezpl[xlsx]' — or save the sheet as .csv"
            ) from e
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        if sheet is not None:
            if sheet not in wb.sheetnames:
                raise ValueError(f"{path.name} has no sheet {sheet!r}; it has {wb.sheetnames}")
            ws = wb[sheet]
        else:
            ws = wb.worksheets[0]
        used = str(ws.title)
        rows = [[_cell(v) for v in r] for r in ws.iter_rows(values_only=True)]
    else:
        text = path.read_text(encoding="utf-8-sig")
        first = text.split("\n", 1)[0]
        rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=sniff_delimiter(first))]
        used = None
    while rows and not any(c.strip() for c in rows[-1]):
        rows.pop()
    return rows, used


def _cell(v: object) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


# -- the media -----------------------------------------------------------------------------------------------------
def _area_fits(m: Media, size_in: tuple[float, float], tol_in: float = 0.04) -> bool:
    """Every print area of the media is the design's label within the tolerance (1 mm)."""
    return bool(m.areas_in) and all(
        abs(w - size_in[0]) <= tol_in and abs(h - size_in[1]) <= tol_in for _x, _y, w, h in m.areas_in
    )


def media_for_ezpx(
    design: EzpxDesign, reg: Registry | None, name: str | None = None
) -> tuple[Media, str, str]:
    """The media the design prints on: ``name`` from the registry (its print areas must be the design's label,
    ±1 mm), else the one registry media whose areas are that label on that page, else — one across only — a
    media derived from the design itself (the note then carries the YAML to put it on record). Returns the
    media, its registry key and a note."""
    size_in = (design.label_mm[0] / 25.4, design.label_mm[1] / 25.4)
    if name is not None:
        if reg is None or name not in reg.media:
            raise ValueError(f"unknown media {name!r}")
        m = reg.media[name]
        if not _area_fits(m, size_in):
            areas = ", ".join(f"{w:.3f} × {h:.3f} in" for _x, _y, w, h in m.areas_in) or "none"
            raise ValueError(
                f"media {name!r}: its print area(s) ({areas}) are not the design's label "
                f"({size_in[0]:.3f} × {size_in[1]:.3f} in = {design.label_mm[0]:g} × {design.label_mm[1]:g} mm)"
            )
        scale = (
            ""
            if m.dpi == design.dpi
            else f"; the design is at {design.dpi} dpi, the media at {m.dpi}: coordinates scaled"
        )
        return m, name, f"media: {name} (named){scale}"
    if reg is not None:
        hits = [
            n
            for n, m in reg.media.items()
            if _area_fits(m, size_in)
            and abs(m.gap_mm - design.gap_mm) < 0.6
            and m.dpi == design.dpi
            and len(m.areas_in) == design.across
        ]
        if len(hits) > 1:
            raise ValueError(f"the design's label fits {len(hits)} media: {hits} — name one with --media")
        if hits:
            return reg.media[hits[0]], hits[0], f"media: {hits[0]} (matched the design's label and page)"
    if design.across != 1:
        raise ValueError(
            f"the design prints {design.across} labels across and no media on record has {design.across} print "
            f"areas of {design.label_mm[0]:g} × {design.label_mm[1]:g} mm — add one to printers.yaml and name it with --media"
        )
    w, ln = design.label_mm
    mname = f"golabel-{w:g}x{ln:g}mm"
    m = Media(
        name=mname,
        dpi=design.dpi,
        width_mm=w,
        length_mm=ln,
        gap_mm=design.gap_mm,
        speed=design.speed,
        darkness=design.darkness,
        stop_mm=design.stop_mm,
        areas_in=((0.0, 0.0, round(size_in[0], 4), round(size_in[1], 4)),),
        labels_in=((0.0, 0.0, round(size_in[0], 4), round(size_in[1], 4)),),
    )
    yaml = (
        f"media:\n  {mname}:\n    dpi: {m.dpi}\n    width_mm: {w:g}\n    length_mm: {ln:g}\n    gap_mm: {m.gap_mm:g}\n"
        f"    areas_in: [[0, 0, {size_in[0]:.4f}, {size_in[1]:.4f}]]"
    )
    return (
        m,
        mname,
        (
            f"media: none on record has this design's label — the job is written for the design itself "
            f"({w:g} × {ln:g} mm, gap {m.gap_mm:g} mm, {m.dpi} dpi); to print it, put the media on record:\n{yaml}"
        ),
    )


def media_for_pemx(
    design: PemxDesign, reg: Registry | None, name: str | None = None
) -> tuple[Media, str, str]:
    """The media for an Easy-Mark project: ``name`` (its print areas must be the project's label, ±1 mm), else
    the one registry media named after the part whose areas are that label (the stock's dpi is the printer's,
    not the file's — two resolutions on record is an ambiguity to resolve by name)."""
    if name is not None:
        if reg is None or name not in reg.media:
            raise ValueError(f"unknown media {name!r}")
        m = reg.media[name]
        if not _area_fits(m, design.label_in):
            areas = ", ".join(f"{w:.3f} × {h:.3f} in" for _x, _y, w, h in m.areas_in) or "none"
            raise ValueError(
                f"media {name!r}: its print area(s) ({areas}) are not the project's label "
                f"({design.label_in[0]:g} × {design.label_in[1]:g} in)"
            )
        return m, name, f"media: {name} (named)"
    slug = re.sub(r"[^a-z0-9]+", "", design.part.lower())
    hits = [
        n
        for n, m in (reg.media.items() if reg else [])
        if slug and slug in re.sub(r"[^a-z0-9]+", "", n.lower()) and _area_fits(m, design.label_in)
    ]
    if len(hits) == 1:
        return reg.media[hits[0]], hits[0], f"media: {hits[0]} (the one on record for {design.part})"  # type: ignore[union-attr]
    if hits:
        raise ValueError(
            f"{len(hits)} media on record for {design.part}: {hits} — name one with --media (the dpi is the printer's)"
        )
    raise ValueError(
        f"no media on record for {design.part} with a {design.label_in[0]:g} × {design.label_in[1]:g} in print area — "
        f"add one to printers.yaml, or name one with --media"
    )


# -- drawing -------------------------------------------------------------------------------------------------------
def _wrap_words(line: str, font, width: float) -> tuple[list[str], bool]:
    """Greedy word wrap to ``width``; a single word wider than the box stays whole (clipped: True)."""
    if font.getlength(line) <= width:
        return [line], False
    out: list[str] = []
    cur = ""
    clipped = False
    for wd in line.split(" "):
        cand = f"{cur} {wd}" if cur else wd
        if font.getlength(cand) <= width:
            cur = cand
        else:
            if cur:
                out.append(cur)
            cur = wd
            if font.getlength(wd) > width:
                clipped = True
    out.append(cur)
    return out, clipped


def _wrap_chars(line: str, font, width: float) -> list[str]:
    """Character wrap to ``width`` (Easy-Mark's ``wordWrap: char``)."""
    out: list[str] = []
    while font.getlength(line) > width and len(line) > 1:
        i = len(line) - 1
        while i > 1 and font.getlength(line[:i]) > width:
            i -= 1
        out.append(line[:i])
        line = line[i:]
    out.append(line)
    return out


def _draw_lines(
    canvas,
    box: tuple[int, int, int, int],
    lines: list[str],
    font,
    px: int,
    lh: int,
    align: str,
    valign: str,
    rotate: bool,
) -> bool:
    """Draw ``lines`` into ``box`` = (x, y, w, h) of ``canvas`` (an 'L' image), one slot of ``lh`` dots per line
    from the top (``valign`` top) or centred (``middle``), each line placed by ``align``; the lines that do not
    fit the height are left out (returns True then). ``rotate`` turns the box's text round inside its rectangle.
    Ink is added to what the canvas already has (boxes may overlap)."""
    from PIL import Image, ImageDraw, ImageOps

    x, y, w, h = box
    if w <= 0 or h <= 0 or not lines:
        return False
    img = Image.new("L", (w, h), 255)
    d = ImageDraw.Draw(img)
    n_fit = max(0, h // lh) if lh > 0 else len(lines)
    clipped = len(lines) > n_fit
    lines = lines[:n_fit]
    y0 = 0 if valign == "top" else max(0, (h - lh * len(lines)) // 2)
    for i, t in enumerate(lines):
        tw = font.getlength(t)
        tx = 0.0 if align == "left" else (w - tw if align == "right" else (w - tw) / 2)
        d.text((tx, y0 + i * lh + (lh - px) / 2), t, fill=0, font=font)
    if rotate:
        img = img.rotate(180)
    canvas.paste(0, (x, y), mask=ImageOps.invert(img))
    return clipped


# -- rendering -----------------------------------------------------------------------------------------------------
def _face_resolver(substitute: str | None, notes: list[str]) -> Callable[[str, float], str]:
    """A face name → a font path: the face itself when this machine has it, else ``substitute`` (named in the
    notes), else a refusal. Each face is reported once."""
    seen: dict[str, str] = {}

    def resolve(face: str, pt: float) -> str:
        if face in seen:
            return seen[face]
        path = find_font(face)
        if path is not None:
            notes.append(f"face {face!r} {pt:g} pt → {path}")
        else:
            if not substitute:
                raise ValueError(
                    f"face {face!r} is not on this machine; name a substitute with --font (the summary will say so) — "
                    f"`rasterezpl media` and the page's face list show what is"
                )
            sub = find_font(substitute)
            if sub is None:
                raise ValueError(f"substitute face {substitute!r} is not on this machine either")
            notes.append(
                f"face {face!r} {pt:g} pt is NOT on this machine → substituted {substitute!r} ({sub}), as asked"
            )
            path = sub
        seen[face] = path
        return path

    return resolve


def _ezpx_lines(box: Box, row: list[str] | None, blank: list[str] | None = None) -> list[str]:
    """The text of a box for one data row (``^F0n`` → column n, zero-based), or its preview text without data.
    A field bound to a column the data lacks is a refusal — or, when ``blank`` is given (``partial``), prints
    empty and the fact is appended to ``blank``."""
    if row is None:
        return box.disp.split("\n") if box.disp else [""]

    def col(m: re.Match[str]) -> str:
        i = int(m.group(1))
        if i >= len(row):
            what = f"{box.label()} binds ^F{i:02d} = column {i + 1} (zero-based {i}), but the data has {len(row)} column(s)"
            if blank is None:
                raise ValueError(what)
            if what not in blank:
                blank.append(what)
            return ""
        return row[i]

    return [FIELD.sub(col, ln) for ln in box.data.split("\n")]


def render_ezpx(
    design: EzpxDesign,
    m: Media,
    rows: list[list[str]] | None = None,
    substitute: str | None = None,
    partial: bool = False,
    notes: list[str] | None = None,
) -> Imported:
    """The job for a GoLabel design on media ``m`` (see :func:`media_for_ezpx`): one label per data row (the
    preview texts alone when ``rows`` is None), filling the media's print areas in order, ``Copy`` (or the
    CopyField column) copies each. A box whose rectangle leaves the label, a box GoLabel would not print, and a
    shape kind not drawn here are refusals — ``partial`` leaves them out instead and says so."""
    notes = notes if notes is not None else []
    notes.insert(0, design.describe())
    if design.db_path or design.db_format:
        notes.append(
            f"data source in the file: {design.db_format or 'database'} {design.db_path!r}"
            + (f", sheet {design.sheet!r}" if design.sheet else "")
            + f", header row: {'yes' if design.header else 'no'} — not opened; the rows come from --data"
        )
    if rows is None:
        notes.append("no --data: ONE label from the preview texts saved in the file (DispData)")
    elif design.row_index is not None and design.row_index + 1 != len(rows):
        notes.append(f"the file was saved with {design.row_index + 1} data row(s); --data has {len(rows)}")
    cf = (
        f" (CopyField ^F{design.copy_field:02d} read, never rewritten)"
        if design.copy_field is not None
        else ""
    )
    if design.copies_from_data and design.copy_field is not None:
        notes.append(f"copies per label: from column {design.copy_field + 1} of the data{cf}")
    else:
        notes.append(f"copies per label: {design.copies}{cf}")
    if any(design.margins_mm):
        notes.append(
            f"Setup LeftMargin {design.margins_mm[0]:g} / TopMargin {design.margins_mm[1]:g} mm: not applied (what GoLabel does with them is unverified)"
        )
    if design.speed is not None or design.darkness is not None or design.stop_mm is not None:
        own = m.speed == design.speed and m.darkness == design.darkness and m.stop_mm == design.stop_mm
        notes.append(
            f"print settings in the design: speed {design.speed}, darkness {design.darkness}, stop {design.stop_mm}"
            + (
                " → in the job"
                if own
                else f" → the media's rule (speed {m.speed}, darkness {m.darkness}, stop {m.stop_mm}; None = the printer's own)"
            )
        )
    problems: list[str] = []
    for s in design.skipped:
        problems.append(f"{s}: not a text box — this importer draws text boxes only")
    W, L = design.label_mm[0] / 25.4 * design.dpi, design.label_mm[1] / 25.4 * design.dpi
    drawn: list[Box] = []
    for b in design.boxes:
        if not b.printed:
            notes.append(f"{b.label()}: IsPrint=false — not printed, as in GoLabel")
            continue
        x, y, w, h = b.rect
        if x < -0.5 or y < -0.5 or x + w > W + 0.5 or y + h > L + 0.5:
            problems.append(
                f"{b.label()} at x {x:.0f}–{x + w:.0f}, y {y:.0f}–{y + h:.0f} leaves the label ({W:.0f} × {L:.0f} dots): "
                f"GoLabel prints nothing for it"
            )
            continue
        drawn.append(b)
    if problems:
        if not partial:
            raise ValueError(
                "\n".join(problems)
                + "\n→ fix the design in GoLabel, or pass --partial to leave these out (the summary will say so)"
            )
        notes.extend(f"LEFT OUT: {p}" for p in problems)
    resolve = _face_resolver(substitute, notes)
    fonts = {b.n: resolve(b.face, b.pt) for b in drawn}
    scale = m.dpi / design.dpi
    per = m.labels_per_block
    wrapped: list[str] = []
    clipped: list[str] = []
    blank: list[str] | None = [] if partial else None
    from PIL import Image

    def one(row: list[str] | None):
        _, _, aw, ah = m.area_px(0)
        img = Image.new("L", (aw, ah), 255)
        for b in drawn:
            x, y, w, h = (v * scale for v in b.rect)
            px = pt_to_px(b.pt, m.dpi)
            font = load_font(fonts[b.n], px)
            lh = max(1, round(b.line_height * scale)) if b.line_height else max(1, round(px * 1.2))
            lines: list[str] = []
            for ln in _ezpx_lines(b, row, blank):
                if b.wrap:
                    parts, clip = _wrap_words(ln, font, w)
                    if len(parts) > 1 and b.label() not in wrapped:
                        wrapped.append(b.label())
                    if clip and b.label() not in clipped:
                        clipped.append(b.label())
                    lines.extend(parts)
                else:
                    if font.getlength(ln) > w and b.label() not in clipped:
                        clipped.append(b.label())
                    lines.append(ln)
            if _draw_lines(
                img, (round(x), round(y), round(w), round(h)), lines, font, px, lh, b.align, "top", b.rotated
            ):
                if b.label() not in clipped:
                    clipped.append(b.label())
        return img

    labels: list = []
    for row in rows if rows is not None else [None]:
        n = design.copies
        if row is not None and design.copies_from_data and design.copy_field is not None:
            i = design.copy_field
            if i >= len(row):
                raise ValueError(
                    f"CopyField ^F{i:02d} = column {i + 1}, but the data has {len(row)} column(s)"
                )
            try:
                n = max(0, int(float(row[i] or 0)))
            except ValueError as e:
                raise ValueError(f"CopyField column {i + 1} holds {row[i]!r}, not a count") from e
        if n:
            img = one(row)
            labels.extend([img] * n)
    if not labels:
        raise ValueError("no labels: every row asks for 0 copies")
    for b_label in wrapped:
        notes.append(f"{b_label}: a line wider than its box was wrapped (GoLabel wraps by its own rules)")
    for what in blank or []:
        notes.append(f"LEFT OUT: {what}: printed blank")
    for b_label in clipped:
        notes.append(
            f"{b_label}: text wider or taller than its box — CLIPPED here; GoLabel clips by its own rules"
        )
    for k in range(1, per):  # the same image for every print area of a row
        if m.area_px(k)[2:] != m.area_px(0)[2:]:
            raise ValueError(
                f"print area {k} of {m.name} differs in size from area 0; the design is one label"
            )
    blocks = [labels[i : i + per] for i in range(0, len(labels), per)]
    notes.append(DISCLAIMER)
    return Imported(job(m, blocks), m, len(labels), notes, "GoLabel .ezpx")


def render_pemx(
    design: PemxDesign, m: Media, substitute: str | None = None, notes: list[str] | None = None
) -> Imported:
    """The job for an Easy-Mark project on media ``m`` (see :func:`media_for_pemx`): every series text a label
    in the project's face and size, centred in the print area, wrapped by character to its width as Easy-Mark
    does; in wire mode a short text is repeated down the area."""
    notes = notes if notes is not None else []
    notes.insert(0, design.describe())
    if design.pt <= 0:
        raise ValueError("the project names no point size")
    resolve = _face_resolver(substitute, notes)
    font_path = resolve(design.face, design.pt)
    px = pt_to_px(design.pt, m.dpi)
    font = load_font(font_path, px)
    lh = max(1, round(px * 1.2))
    _, _, aw, ah = m.area_px(0)
    from PIL import Image

    wrapped = 0
    clipped = 0
    imgs = []
    for t in design.texts:
        lines: list[str] = []
        for ln in t.replace("\r\n", "\n").split("\n"):
            parts = _wrap_chars(ln, font, aw)
            wrapped += len(parts) > 1
            lines.extend(parts)
        if design.wire_mode and lines and lh * len(lines) * 2 <= ah:
            lines = lines * (ah // (lh * len(lines)))
        img = Image.new("L", (aw, ah), 255)
        clipped += _draw_lines(img, (0, 0, aw, ah), lines, font, px, lh, design.align, "middle", False)
        imgs.append(img)
    if wrapped:
        notes.append(
            f"{wrapped} line(s) wider than the print area were wrapped by character (Easy-Mark wraps by its own rules)"
        )
    if clipped:
        notes.append(
            f"{clipped} label(s) have more lines than fit the print area — CLIPPED here; Easy-Mark clips by its own rules"
        )
    if design.wire_mode:
        notes.append(
            "wire mode: a text shorter than the print area is repeated down it, as Easy-Mark does — the count may differ"
        )
    per = m.labels_per_block
    for k in range(1, per):
        if m.area_px(k)[2:] != (aw, ah):
            raise ValueError(
                f"print area {k} of {m.name} differs in size from area 0; the design is one label"
            )
    blocks = [imgs[i : i + per] for i in range(0, len(imgs), per)]
    notes.append(DISCLAIMER)
    return Imported(job(m, blocks), m, len(imgs), notes, "Easy-Mark Plus .pemx")


# -- one entry point -----------------------------------------------------------------------------------------------
def import_design(
    path: Path,
    reg: Registry | None,
    media: str | None = None,
    data: Path | None = None,
    sheet: str | None = None,
    header: bool | None = None,
    substitute: str | None = None,
    partial: bool = False,
) -> Imported:
    """``path`` (.ezpx or .pemx) → an :class:`Imported` job. ``data`` = the rows for a GoLabel design (CSV or
    .xlsx; ``sheet`` by name, default the one the design names when the workbook has it, else the first;
    ``header`` overrides the design's own header flag); ``media`` names a registry media; ``substitute`` the
    face to use for a face this machine lacks; ``partial`` leaves out what cannot be drawn instead of refusing."""
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    if suffix == ".ezpx":
        design = read_ezpx(raw)
        notes: list[str] = []
        m, key, mnote = media_for_ezpx(design, reg, media)
        notes.append(mnote)
        rows: list[list[str]] | None = None
        if data is not None:
            want = sheet if sheet is not None else design.sheet
            try:
                rows, used = read_table(data, want)
            except ValueError:
                if sheet is not None:
                    raise
                rows, used = read_table(data, None)
                notes.append(
                    f"{data.name} has no sheet {want!r} (the one the design names); the first sheet was used"
                )
            if used is not None:
                notes.append(f"data: {data.name}, sheet {used!r}, {len(rows)} row(s)")
            else:
                notes.append(f"data: {data.name}, {len(rows)} row(s)")
            has_header = design.header if header is None else header
            if has_header:
                if not rows:
                    raise ValueError(f"{data.name} is empty")
                head, rows = rows[0], rows[1:]
                notes.append(
                    f"header row dropped ({', '.join(h for h in head if h) or 'blank'}); ^F00 = the first column"
                )
            if not rows:
                raise ValueError(f"{data.name} has no data rows")
        elif sheet is not None or header is not None:
            raise ValueError("--sheet / --header need --data")
        out = render_ezpx(design, m, rows, substitute, partial, notes)
        out.media_name = key
        return out
    if suffix == ".pemx":
        if data is not None or sheet is not None or header is not None:
            raise ValueError("an Easy-Mark project carries its own texts: --data does not apply")
        pdesign = read_pemx(raw)
        m, key, mnote = media_for_pemx(pdesign, reg, media)
        out = render_pemx(pdesign, m, substitute, [mnote])
        out.media_name = key
        return out
    raise ValueError(f"{path.name}: not a .ezpx (GoLabel) or .pemx (Easy-Mark Plus) file")
