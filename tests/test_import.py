"""Importing a GoLabel .ezpx and an Easy-Mark Plus .pemx — SYNTHETIC files only (a site's project is its own).

The fixtures mirror the real files' structure as learned writing them (see importers.py): a PrintJob with a
Setup in mm, GraphicShape text boxes with FontCmd / BoundRect / Direction / Data (^F0n) / DispData; a zip of
project.json with a format, series data and a wire-mode flag."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from rasterezpl.importers import (
    DISCLAIMER,
    import_design,
    media_for_ezpx,
    media_for_pemx,
    read_ezpx,
    read_pemx,
    read_table,
    render_ezpx,
    render_pemx,
)
from rasterezpl.registry import load
from rasterezpl.stream import count_labels, decode_block, header_of, parse_blocks

PIL = pytest.importorskip("PIL")

# a 50 × 25 mm flag at 203 dpi = 400 × 200 dots, alone and two across (the gap between the two is 4 mm)
REG = """
media:
  flag:
    dpi: 203
    width_mm: 50
    length_mm: 25
    gap_mm: 2
    areas_in: [[0, 0, 1.9685, 0.9843]]
  flag-2up:
    dpi: 203
    width_mm: 104
    length_mm: 25
    gap_mm: 2
    areas_in: [[0, 0, 1.9685, 0.9843], [2.1, 0, 1.9685, 0.9843]]
  other:
    dpi: 203
    width_mm: 30
    length_mm: 20
    gap_mm: 2
    areas_in: [[0, 0, 1.1811, 0.7874]]
"""


def box(
    n: int,
    data: str,
    disp: str,
    x: float,
    y: float,
    w: float,
    h: float,
    face: str = "Inter",
    pt: float = 10,
    angle: str = "Angle0",
    align: str = "Center",
    lh: int | None = None,
    wrap: str = "WordWrap",
    is_print: str = "true",
    kind: str = "WindowTextBox",
) -> str:
    """One GraphicShape as GoLabel writes it: the anchor X/Y is the rectangle's top-left corner upright and its
    bottom-right corner turned round (Angle180); the BoundRect is the rectangle either way."""
    ax, ay = (x + w, y + h) if angle == "Angle180" else (x, y)
    lh_attr = f' LineHeight="{lh}"' if lh is not None else ""
    data_xml = data.replace("\n", "&#xD;&#xA;")
    disp_xml = disp.replace("\n", "&#xD;&#xA;")
    return f"""      <GraphicShape xsi:type="{kind}" Style="Cross" IsPrint="{is_print}" Locked="false" Direction="{angle}" X="{ax}" Y="{ay}" Alignment="{align}" AlignPointX="{ax}" AlignPointY="{ay}" FontCmd="{face},{pt}&#xD;&#xA;" LineSpacing="Single"{lh_attr} FontWidth="24" BTrueType="true">
        <Id>{n}</Id>
        <Name>W</Name>
        <BoundRectWidth>{w}</BoundRectWidth>
        <DispData>{disp_xml}</DispData>
        <Data>{data_xml}</Data>
        <BoundRectHeight>{h}</BoundRectHeight>
        <BoundRect>
          <Location>
            <X>{x}</X>
            <Y>{y}</Y>
          </Location>
          <Size>
            <Width>{w}</Width>
            <Height>{h}</Height>
          </Size>
        </BoundRect>
        <WrapMode>{wrap}</WrapMode>
      </GraphicShape>
"""


def ezpx(
    boxes: str,
    w_mm: float = 50,
    l_mm: float = 25,
    gap_mm: float = 2,
    dpi: int = 203,
    across: int = 1,
    copy: int = 1,
    copy_db: str = "false",
    copy_field: str = "^F07",
    no_header: str = "false",
    row_index: int = 2,
    speed: int = 2,
    darkness: int = 14,
) -> bytes:
    xml = f"""<?xml version="1.0" encoding="utf-8"?>
<PrintJob xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <FormatVersion>1</FormatVersion>
  <QLabelSDKVersion>1.2.5.2</QLabelSDKVersion>
  <Label>
    <qlabel>
{boxes}    </qlabel>
    <DataBaseFormat>ExcelXlsx</DataBaseFormat>
    <DataBaseFilePath>C:\\Users\\designer\\Desktop\\labels.xlsx</DataBaseFilePath>
    <DataBaseSelection>SELECT  * from `sheet1$` ;</DataBaseSelection>
    <DatabaseNoHeader>{no_header}</DatabaseNoHeader>
    <RowIndex>{row_index}</RowIndex>
  </Label>
  <Setup bInfinityPrint="false" LabelLength="{l_mm}" LabelWidth="{w_mm}" LeftMargin="0" TopMargin="0" LabelType="0" GapLength="{gap_mm}" Speed="{speed}" Copy="{copy}" bCopyDataBase="{copy_db}" CopyField="{copy_field}" Rotate180="255" Stop="18" Darkness="{darkness}" Number="1" PageDirection="Portrait">
    <Layout Shape="2" AcrossType="Copied" HorAcross="{across}" VerAcross="1" HorGap="90" VerGap="0" />
    <Description>Synthetic</Description>
    <UnitType>Mm</UnitType>
    <Dpi>{dpi}</Dpi>
  </Setup>
  <PrinterModel>G500</PrinterModel>
  <PrinterLanguage>EZPL</PrinterLanguage>
</PrintJob>
"""
    return xml.replace("\n", "\r\n").encode("utf-8")


TWO_BOXES = box(1, "^F00", "Alpha", 10, 10, 300, 40) + box(
    2, "^F01\n^F02", "Beta\nGamma", 90, 110, 300, 80, angle="Angle180", pt=9, lh=30
)
ROWS = [["a", "b", "c"], ["r1c1", "r1c2", "r1c3"], ["r2c1", "r2c2", "r2c3"], ["r3c1", "r3c2", "r3c3"]]


def pemx(
    texts: list[str], face: str = "Inter", pt: float = 5.4, wire: bool = True, part: str = "S150X225VATY"
) -> bytes:
    doc = {
        "@type": "project",
        "documents": [
            {
                "@type": "document",
                "name": part,
                "format": {
                    "partName": part,
                    "page": {"layout": {"columns": 2, "rows": 1}, "size": {"width": 3.25, "height": 2.375}},
                    "dimensions": {"label": {"width": 1.5, "height": 0.75}},
                    "unit": "INCHES",
                    "wireMode": wire,
                },
                "labels": [
                    {
                        "inlineContent": [
                            {
                                "content": [{"textValue": t.replace("\n", "&#10;")}],
                                "style": {"textAlign": "center"},
                            }
                        ]
                    }
                    for t in texts
                ],
                "series": [
                    {
                        "labelCount": len(texts),
                        "style": {"fontSize": pt, "fontFamily": face},
                        "data": list(texts),
                    }
                ],
                "wireMode": wire,
            }
        ],
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("project-version.txt", "1.6.0")
        z.writestr("project.json", json.dumps(doc))
    return buf.getvalue()


@pytest.fixture
def reg(tmp_path):
    p = tmp_path / "printers.yaml"
    p.write_text(REG, encoding="utf-8")
    return load(str(p))


def physical_page(data: bytes, m, block: int = 0):
    """The job's block as an 'L' image in the physical frame (what the designer saw)."""
    blocks = parse_blocks(data)
    page = decode_block(blocks[block][1], m).convert("L")
    return page.rotate(180) if m.rotate180 else page


def ink_bbox(img):
    from PIL import ImageOps

    return ImageOps.invert(img).getbbox()


# -- .ezpx ---------------------------------------------------------------------------------------------------------
def test_read_ezpx_geometry_fonts_and_bindings():
    d = read_ezpx(ezpx(TWO_BOXES))
    assert d.label_mm == (50, 25) and d.gap_mm == 2 and d.dpi == 203 and d.across == 1
    assert (d.width_px, d.length_px) == (400, 200)
    assert d.header is True and d.sheet == "sheet1" and d.row_index == 2 and d.printer == "G500"
    assert d.copies == 1 and d.copy_field == 7 and d.copies_from_data is False  # ^F07 read, never rewritten
    assert d.speed == 2 and d.darkness == 14 and d.stop_mm == 18
    b1, b2 = d.boxes
    assert b1.rect == (10, 10, 300, 40) and b1.face == "Inter" and b1.pt == 10 and not b1.rotated
    assert b1.data == "^F00" and b1.disp == "Alpha" and b1.fields() == [0] and b1.align == "center"
    assert b2.rect == (90, 110, 300, 80) and b2.rotated and b2.pt == 9 and b2.line_height == 30
    assert b2.data == "^F01\n^F02" and b2.disp == "Beta\nGamma" and b2.fields() == [1, 2]
    with pytest.raises(ValueError, match="not a GoLabel"):
        read_ezpx(b"<html/>")
    with pytest.raises(ValueError, match="not a GoLabel"):
        read_ezpx(b"nonsense")


def test_fields_bind_zero_based_columns_and_no_data_means_one_preview_label(reg):
    d = read_ezpx(ezpx(box(1, "^F01", "Beta", 10, 10, 300, 40)))
    m = reg.media["flag"]
    preview = render_ezpx(d, m)  # no rows: the DispData, one label
    assert preview.labels == 1 and any("ONE label from the preview" in n for n in preview.notes)
    bound = render_ezpx(d, m, rows=[["x", "Beta", "z"]])  # ^F01 = the SECOND column (zero-based 1)
    assert bound.data == preview.data
    other = render_ezpx(d, m, rows=[["Beta", "x", "z"]])  # were it one-based this would match instead
    assert other.data != preview.data
    with pytest.raises(ValueError, match=r"\^F01 = column 2 .*zero-based 1.*but the data has 1 column"):
        render_ezpx(d, m, rows=[["only"]])
    blank = render_ezpx(d, m, rows=[["only"]], partial=True)  # a real template bound ^F06 of a 6-column sheet
    assert blank.labels == 1 and ink_bbox(physical_page(blank.data, m)) is None
    assert any(
        n.startswith("LEFT OUT: box 1 ('^F01', preview 'Beta') binds ^F01 = column 2")
        and n.endswith(": printed blank")
        for n in blank.notes
    )


def test_ink_stays_inside_the_boxes_and_angle180_turns_the_text_round(reg):
    m = reg.media["flag"]
    d = read_ezpx(ezpx(TWO_BOXES))
    imp = render_ezpx(d, m, rows=ROWS[1:])
    assert imp.labels == 3 and imp.blocks == 3 and imp.media is m
    page = physical_page(imp.data, m)
    assert page.size == (400, 200)
    # every dot of ink lies in one of the two rectangles
    from PIL import Image, ImageOps

    mask = Image.new("L", page.size, 0)
    for x, y, w, h in (b.rect for b in d.boxes):
        mask.paste(255, (int(x), int(y), int(x + w), int(y + h)))
    ink = ImageOps.invert(page)
    assert ink.getbbox() is not None
    outside = Image.composite(ink, Image.new("L", page.size, 0), ImageOps.invert(mask))
    assert outside.getbbox() is None
    # the turned box: its crop equals the same box drawn upright, rotated 180° — the text reads upside down
    upright = read_ezpx(ezpx(box(2, "^F01\n^F02", "Beta\nGamma", 90, 110, 300, 80, pt=9, lh=30)))
    turned = read_ezpx(
        ezpx(box(2, "^F01\n^F02", "Beta\nGamma", 90, 110, 300, 80, pt=9, lh=30, angle="Angle180"))
    )
    pu = physical_page(render_ezpx(upright, m, rows=ROWS[1:2]).data, m).crop((90, 110, 390, 190))
    pt = physical_page(render_ezpx(turned, m, rows=ROWS[1:2]).data, m).crop((90, 110, 390, 190))
    assert pt.tobytes() == pu.rotate(180).tobytes() and ink_bbox(pu) is not None


def test_a_box_off_the_label_or_a_shape_that_is_not_text_is_a_refusal_unless_partial(reg):
    m = reg.media["flag"]
    off = ezpx(box(1, "^F00", "Alpha", 10, 10, 200, 40) + box(2, "^F01", "Beta", 300, 150, 200, 40))
    d = read_ezpx(off)
    with pytest.raises(
        ValueError,
        match=r"box 2 \('\^F01', preview 'Beta'\) at x 300–500, y 150–190 leaves the label \(400 × 200 dots\)",
    ) as ei:
        render_ezpx(d, m, rows=ROWS[1:])
    assert "GoLabel prints nothing for it" in str(ei.value) and "--partial" in str(ei.value)
    imp = render_ezpx(d, m, rows=ROWS[1:], partial=True)
    assert imp.labels == 3 and any(n.startswith("LEFT OUT: box 2") for n in imp.notes)
    assert ink_bbox(physical_page(imp.data, m)) is not None
    d2 = read_ezpx(
        ezpx(
            box(1, "^F00", "Alpha", 10, 10, 200, 40)
            + box(2, "^F01", "Beta", 10, 100, 200, 40, kind="Barcode")
        )
    )
    assert d2.skipped == ["shape 2: Barcode"] and len(d2.boxes) == 1
    with pytest.raises(ValueError, match="shape 2: Barcode: not a text box"):
        render_ezpx(d2, m)
    imp2 = render_ezpx(d2, m, partial=True)
    assert any("LEFT OUT: shape 2: Barcode" in n for n in imp2.notes)
    # IsPrint=false is GoLabel's own "not printed": no refusal, a note
    d3 = read_ezpx(ezpx(box(1, "^F00", "Alpha", 10, 10, 200, 40, is_print="false")))
    imp3 = render_ezpx(d3, m)
    assert ink_bbox(physical_page(imp3.data, m)) is None and any("IsPrint=false" in n for n in imp3.notes)


def test_a_missing_face_is_a_refusal_and_a_substitute_is_announced(reg):
    m = reg.media["flag"]
    d = read_ezpx(ezpx(box(1, "^F00", "Alpha", 10, 10, 300, 40, face="No Such Face 123")))
    with pytest.raises(
        ValueError, match="face 'No Such Face 123' is not on this machine; name a substitute with --font"
    ):
        render_ezpx(d, m)
    imp = render_ezpx(d, m, substitute="Inter")
    assert any(
        n.startswith("face 'No Such Face 123' 10 pt is NOT on this machine → substituted 'Inter'")
        for n in imp.notes
    )
    with pytest.raises(ValueError, match="substitute face 'Nope' is not on this machine either"):
        render_ezpx(d, m, substitute="Nope")
    ok = render_ezpx(read_ezpx(ezpx(box(1, "^F00", "Alpha", 10, 10, 300, 40))), m)
    assert any(n.startswith("face 'Inter' 10 pt → ") and n.endswith("Inter-Regular.ttf") for n in ok.notes)
    assert ok.notes[-1] == DISCLAIMER  # always, and last


def test_copies_from_setup_and_from_the_copy_field(reg):
    m = reg.media["flag"]
    two = read_ezpx(ezpx(box(1, "^F00", "Alpha", 10, 10, 300, 40), copy=2))
    imp = render_ezpx(two, m, rows=ROWS[1:])
    assert imp.labels == 6 and any(
        n == "copies per label: 2 (CopyField ^F07 read, never rewritten)" for n in imp.notes
    )
    page1, page2 = physical_page(imp.data, m, 0), physical_page(imp.data, m, 1)
    assert page1.tobytes() == page2.tobytes()
    db = read_ezpx(ezpx(box(1, "^F00", "Alpha", 10, 10, 300, 40), copy_db="true", copy_field="^F02"))
    assert db.copies_from_data and db.copy_field == 2
    imp = render_ezpx(db, m, rows=[["a", "b", "1"], ["c", "d", "0"], ["e", "f", "3"]])
    assert imp.labels == 4 and any(
        n.startswith("copies per label: from column 3 of the data") for n in imp.notes
    )
    with pytest.raises(ValueError, match="CopyField column 3 holds 'many', not a count"):
        render_ezpx(db, m, rows=[["a", "b", "many"]])
    with pytest.raises(ValueError, match="every row asks for 0 copies"):
        render_ezpx(db, m, rows=[["a", "b", "0"]])


def test_media_named_matched_or_derived(reg):
    d = read_ezpx(ezpx(TWO_BOXES))
    # named: its areas must be the design's label
    m, key, note = media_for_ezpx(d, reg, "flag-2up")
    assert m.name == key == "flag-2up" and note == "media: flag-2up (named)"
    with pytest.raises(
        ValueError, match="media 'other': its print area.* are not the design's label .*50 × 25 mm"
    ):
        media_for_ezpx(d, reg, "other")
    # matched: the one media on record with that label, gap and dpi, one across
    m, key, note = media_for_ezpx(d, reg, None)
    assert m.name == "flag" and "matched" in note
    imp = render_ezpx(d, reg.media["flag-2up"], rows=ROWS[1:])
    assert imp.labels == 3 and imp.blocks == 2  # two across: the areas filled in order
    assert header_of(imp.data) == (25, 2, 104)
    # derived: nothing on record for a 60 × 30 label → the design's own media, with the YAML to put it on record
    d2 = read_ezpx(ezpx(box(1, "^F00", "Alpha", 10, 10, 300, 40), w_mm=60, l_mm=30, gap_mm=3))
    m2, key2, note2 = media_for_ezpx(d2, reg, None)
    assert m2.name == key2 == "golabel-60x30mm" and (m2.width_mm, m2.length_mm, m2.gap_mm, m2.dpi) == (
        60,
        30,
        3,
        203,
    )
    assert m2.areas_in == ((0.0, 0.0, 2.3622, 1.1811),) and (m2.speed, m2.darkness, m2.stop_mm) == (2, 14, 18)
    assert "put the media on record" in note2 and "areas_in: [[0, 0, 2.3622, 1.1811]]" in note2
    imp2 = render_ezpx(d2, m2, rows=ROWS[1:])
    assert imp2.data.startswith(b"^Q30,3\r^W60\r~R200\r^AT\r^S2\r^H14\r^E18\r^P1\r^L\r")
    assert any(n.endswith("→ in the job") for n in imp2.notes)
    with pytest.raises(ValueError, match="2 labels across and no media on record"):
        media_for_ezpx(read_ezpx(ezpx(TWO_BOXES, w_mm=60, across=2)), reg, None)
    # two across with a matching 2-up on record: matched by the across count too
    d3 = read_ezpx(ezpx(TWO_BOXES, across=2))
    assert media_for_ezpx(d3, reg, None)[0].name == "flag-2up"


def test_wrapping_and_clipping_are_reported_never_silent(reg):
    m = reg.media["flag"]
    narrow = read_ezpx(
        ezpx(box(1, "^F00", "a line that is far too long for this little box", 10, 10, 120, 100, pt=10))
    )
    imp = render_ezpx(narrow, m)
    assert any("wrapped" in n and "box 1" in n for n in imp.notes)
    nowrap = read_ezpx(
        ezpx(
            box(1, "^F00", "a line that is far too long for this little box", 10, 10, 120, 40, wrap="NoWrap")
        )
    )
    imp = render_ezpx(nowrap, m)
    assert any("CLIPPED" in n and "box 1" in n for n in imp.notes)
    # clipped: the ink stays inside the box all the same
    page = physical_page(imp.data, m)
    x0, y0, x1, y1 = ink_bbox(page)
    assert 10 <= x0 and x1 <= 130 and 10 <= y0 and y1 <= 50
    tall = read_ezpx(ezpx(box(1, "^F00", "one\ntwo\nthree\nfour", 10, 10, 300, 40, lh=30)))
    imp = render_ezpx(tall, m)
    assert any("CLIPPED" in n for n in imp.notes)


def test_tables_csv_and_xlsx(tmp_path):
    csv_p = tmp_path / "rows.csv"
    csv_p.write_bytes("\ufeffa;b;c\r\n1;2;3\r\n\r\n4;5;6\r\n\r\n".encode("utf-8"))  # BOM, CRLF, a blank row
    assert read_table(csv_p) == ([["a", "b", "c"], ["1", "2", "3"], ["4", "5", "6"]], None)
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "other"
    ws.append(["x", "y"])
    ws.append([1, 2.0])
    ws2 = wb.create_sheet("sheet1")
    ws2.append(["a", "b", "c"])
    ws2.append([7, "eight", 9.5])
    ws2.append([None, None, None])
    xp = tmp_path / "rows.xlsx"
    wb.save(xp)
    assert read_table(xp) == ([["x", "y"], ["1", "2"]], "other")
    assert read_table(xp, "sheet1") == ([["a", "b", "c"], ["7", "eight", "9.5"]], "sheet1")
    with pytest.raises(ValueError, match="has no sheet 'nope'"):
        read_table(xp, "nope")


def test_import_design_end_to_end_with_data(tmp_path, reg):
    f = tmp_path / "design.ezpx"
    f.write_bytes(ezpx(TWO_BOXES))
    rows = tmp_path / "rows.csv"
    rows.write_text("\n".join(",".join(r) for r in ROWS) + "\n", encoding="utf-8")
    imp = import_design(f, reg, data=rows)
    assert imp.labels == 3 and imp.media.name == "flag"
    assert any(n == "header row dropped (a, b, c); ^F00 = the first column" for n in imp.notes)
    assert any(n.startswith("data source in the file: ExcelXlsx") and "not opened" in n for n in imp.notes)
    assert imp.notes[0].startswith("GoLabel design: label 50 × 25 mm")
    no_head = import_design(f, reg, data=rows, header=False)
    assert no_head.labels == 4  # the header line printed as a label — the operator's call
    with pytest.raises(ValueError, match="--sheet / --header need --data"):
        import_design(f, reg, header=False)
    pytest.importorskip("openpyxl")
    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "data"
    for r in ROWS:
        wb.active.append(r)
    xp = tmp_path / "rows.xlsx"
    wb.save(xp)
    imp = import_design(f, reg, data=xp)  # the design names sheet1; the workbook has only "data"
    assert imp.labels == 3 and any(
        "has no sheet 'sheet1'" in n and "first sheet was used" in n for n in imp.notes
    )
    assert any(n == "data: rows.xlsx, sheet 'data', 4 row(s)" for n in imp.notes)
    with pytest.raises(ValueError, match="has no sheet 'x'"):
        import_design(f, reg, data=xp, sheet="x")


def test_the_imported_rendering_is_the_same_on_every_machine(reg):
    """One rendering everywhere: the synthetic design in the bundled face, hashed. The CI matrix (Ubuntu, Windows,
    macOS × three Pythons) runs this; a change here is a change of what a site's imported labels look like."""
    imp = render_ezpx(read_ezpx(ezpx(TWO_BOXES)), reg.media["flag"], rows=ROWS[1:])
    assert hashlib.sha256(imp.data).hexdigest() == PINNED_EZPX


# -- .pemx ---------------------------------------------------------------------------------------------------------
def test_read_pemx_and_media_matching(reg):
    d = read_pemx(pemx(["l1\nl2", "l3\nl4", "l5"]))
    assert (
        d.part == "S150X225VATY"
        and d.texts == ["l1\nl2", "l3\nl4", "l5"]
        and d.pt == 5.4
        and d.face == "Inter"
    )
    assert d.label_in == (1.5, 0.75) and d.page_in == (3.25, 2.375) and d.columns == 2 and d.wire_mode
    assert d.align == "center" and d.version == "1.6.0"
    assert d.describe().startswith(
        "Easy-Mark project: S150X225VATY, 3 labels, Inter 5.4 pt, label 1.5 × 0.75 in"
    )
    with pytest.raises(ValueError, match="not a zip"):
        read_pemx(b"nope")
    # the presets hold the stock at two resolutions: the printer's dpi is not in the file — name one
    presets = load(None)
    with pytest.raises(ValueError, match="2 media on record for S150X225VATY: .*name one with --media"):
        media_for_pemx(d, presets, None)
    m, key, note = media_for_pemx(d, presets, "panduit-s150x225vaty-2up-203")
    assert m.dpi == 203 and key == "panduit-s150x225vaty-2up-203"
    assert note == "media: panduit-s150x225vaty-2up-203 (named)"
    with pytest.raises(
        ValueError,
        match="media 'panduit-s150x225vaty-2up-full': its print area.* are not the project's label",
    ):
        media_for_pemx(d, presets, "panduit-s150x225vaty-2up-full")
    with pytest.raises(ValueError, match="2 media on record"):
        media_for_pemx(d, reg, None)  # a registry file is loaded on top of the presets: still two
    with pytest.raises(ValueError, match="no media on record for X999 with a 1.5 × 0.75 in print area"):
        media_for_pemx(read_pemx(pemx(["a"], part="X999")), reg, None)
    # one on record for the part → taken
    one = load(None)
    one.media.pop("panduit-s150x225vaty-2up")
    m, key, note = media_for_pemx(d, one, None)
    assert key == "panduit-s150x225vaty-2up-203" and "the one on record for S150X225VATY" in note


def test_render_pemx_labels_wrap_and_wire_mode():
    m = load(None).media["panduit-s150x225vaty-2up-203"]
    d = read_pemx(pemx(["l1\nl2", "l3\nl4", "l5"], wire=False))
    imp = render_pemx(d, m)
    assert (
        imp.labels == 3
        and imp.blocks == 2
        and imp.source == "Easy-Mark Plus .pemx"
        and imp.notes[-1] == DISCLAIMER
    )
    assert not any("wire mode" in n for n in imp.notes)
    # wire mode repeats a short text down the print area: more rows of ink than the single text
    one = render_pemx(read_pemx(pemx(["short"], wire=False)), m)
    wire = render_pemx(read_pemx(pemx(["short"], wire=True)), m)

    def rows_of(imp) -> int:  # the rows of the page that carry ink
        page = physical_page(imp.data, m)
        return sum(1 for y in range(page.size[1]) if ink_bbox(page.crop((0, y, page.size[0], y + 1))))

    assert rows_of(wire) > 3 * rows_of(one) and any("wire mode" in n for n in wire.notes)
    # a line wider than the area is wrapped by character and said so; a face not here needs a substitute
    long = render_pemx(read_pemx(pemx(["x" * 200], wire=False, pt=8)), m)
    assert any("wrapped by character" in n for n in long.notes)
    with pytest.raises(ValueError, match="face 'Nope Sans' is not on this machine"):
        render_pemx(read_pemx(pemx(["a"], face="Nope Sans")), m)
    sub = render_pemx(read_pemx(pemx(["a"], face="Nope Sans")), m, substitute="Inter")
    assert any("substituted 'Inter'" in n for n in sub.notes)


def test_import_design_pemx_refuses_data_and_unknown_suffix(tmp_path):
    f = tmp_path / "p.pemx"
    f.write_bytes(pemx(["a", "b"]))
    with pytest.raises(ValueError, match="carries its own texts"):
        import_design(f, load(None), media="panduit-s150x225vaty-2up-203", data=tmp_path / "x.csv")
    imp = import_design(f, load(None), media="panduit-s150x225vaty-2up-203")
    assert imp.labels == 2 and imp.blocks == 1
    (tmp_path / "x.lbl").write_bytes(b"")
    with pytest.raises(ValueError, match="not a .ezpx .GoLabel. or .pemx"):
        import_design(tmp_path / "x.lbl", load(None))


# -- the command ---------------------------------------------------------------------------------------------------
def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    # the child's console is UTF-8 here whatever the machine's is (a Windows runner's is cp1252 and the summary
    # carries — and ×; the cp1252 fallback itself is test_windows's business)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, "-m", "rasterezpl", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_cli_import_writes_the_job_and_the_proof_and_says_whose_rendering_it_is(tmp_path):
    (tmp_path / "printers.yaml").write_text(REG, encoding="utf-8")
    (tmp_path / "d.ezpx").write_bytes(ezpx(TWO_BOXES))
    (tmp_path / "rows.csv").write_text("\n".join(",".join(r) for r in ROWS) + "\n", encoding="utf-8")
    r = run(
        "--registry",
        "printers.yaml",
        "import",
        "d.ezpx",
        "--data",
        "rows.csv",
        "-o",
        "out.ezpl",
        "--proof",
        "proof.png",
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert "wrote out.ezpl: 3 labels in 3 blocks for flag — from GoLabel .ezpx" in r.stdout
    assert DISCLAIMER in r.stdout and "media: flag (matched" in r.stdout and "header row dropped" in r.stdout
    assert (
        count_labels((tmp_path / "out.ezpl").read_bytes()) == 3
        and (tmp_path / "proof.png").stat().st_size > 0
    )
    assert "wrote proof.png: 3 web rows" in r.stdout
    # a refusal is a message and exit 1, no job written
    (tmp_path / "off.ezpx").write_bytes(ezpx(box(1, "^F00", "Alpha", 300, 10, 300, 40)))
    r = run("--registry", "printers.yaml", "import", "off.ezpx", "-o", "off.ezpl", cwd=tmp_path)
    assert r.returncode == 1 and "leaves the label" in r.stderr and not (tmp_path / "off.ezpl").exists()
    r = run("--registry", "printers.yaml", "import", "off.ezpx", "-o", "off.ezpl", "--partial", cwd=tmp_path)
    assert r.returncode == 0 and "LEFT OUT: box 1" in r.stdout and (tmp_path / "off.ezpl").exists()
    # Easy-Mark
    (tmp_path / "p.pemx").write_bytes(pemx(["a\nb", "c"]))
    r = run(
        "import",
        "p.pemx",
        "--media",
        "panduit-s150x225vaty-2up-203",
        "-o",
        "p.ezpl",
        "--proof",
        "p.pdf",
        cwd=tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert (
        "wrote p.ezpl: 2 labels in 1 blocks for panduit-s150x225vaty-2up-203 — from Easy-Mark Plus .pemx"
        in r.stdout
    )
    assert DISCLAIMER in r.stdout and (tmp_path / "p.pdf").stat().st_size > 0
    r = run("import", "p.pemx", "-o", "p.ezpl", cwd=tmp_path)
    assert r.returncode == 1 and "name one with --media" in r.stderr


PINNED_EZPX = "f3222f682711c258e17bafef7e5f51456105fea72cfcf27f5263bb8b3f9af627"
