"""A GoLabel design to print from GoLabel and from rasterezpl, side by side — the importer's paper check.

    python examples/golabel_compare.py OUTDIR [--width-mm 92] [--length-mm 34] [--gap-mm 2] [--dpi 203]

writes ``OUTDIR/compare-<w>x<l>.ezpx``: a synthetic GoLabel 1.2 project (the element set of a real template,
none of its content) for one label of that size with text boxes in the face GoLabel templates use (Microsoft
YaHei UI, on every Windows machine): a centred 9 pt title, left 8 pt text, two lines at GoLabel's LineHeight,
a line too long for its box (GoLabel wraps it its way), a box turned round (Direction Angle180 — the back half
of a wrap-around flag), and TL / BR marks in two corners so the orientation and placement can be read.

Then, on the Windows machine beside the printer:

    GoLabel:    open the .ezpx, print one label.
    rasterezpl: rasterezpl import OUTDIR/compare-92x34.ezpx --media godex-92x34-flags -o OUTDIR/ours.ezpl --proof OUTDIR/ours.png
                rasterezpl print OUTDIR/ours.ezpl --to "win:Godex G500 (Copy 3)" --media godex-92x34-flags

and lay the two labels over each other. Expected: the same text in the same places; the glyph shapes, the
wrap of the long line and the spacing may differ — the importer says so in every summary.
"""

from __future__ import annotations

import argparse
from pathlib import Path

HEAD = """<?xml version="1.0" encoding="utf-8"?>
<PrintJob xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <FormatVersion>1</FormatVersion>
  <QLabelSDKVersion>1.2.5.2</QLabelSDKVersion>
  <GoLabelZoomFactor>1.2</GoLabelZoomFactor>
  <Label>
    <SerialFormat>
      <string xsi:nil="true" />
      <string />
    </SerialFormat>
    <SerialLeadingCode>
      <int>0</int>
    </SerialLeadingCode>
    <SerialCustomSequence />
    <bSerialSpecialCarry>
      <boolean>false</boolean>
    </bSerialSpecialCarry>
    <VariableFormat />
    <qlabel>
"""
TAIL = """    </qlabel>
    <VariableOpFormat />
    <VariableOption />
    <DateFormat>y2-me-dd</DateFormat>
    <TimeFormat>h:m:s</TimeFormat>
    <DataBaseFormat>ExcelXlsx</DataBaseFormat>
    <DataBaseFilePath />
    <DataBaseSelection />
    <UserID />
    <Password />
    <EncryptPwd>false</EncryptPwd>
    <DatabaseNoHeader>false</DatabaseNoHeader>
    <ServerName />
    <IntegratedSecurity>false</IntegratedSecurity>
    <RowIndex>0</RowIndex>
  </Label>
  <Setup bInfinityPrint="false" LabelLength="{length}" LabelWidth="{width}" LeftMargin="0" TopMargin="0" LabelType="0" GapLength="{gap}" FeedLength="0" ZSign="45" BlackMark="0" Position="0" Speed="3" Copy="1" bCopyDataBase="false" CopyField="None" Stripper="0" LabelsPerCut="0" DoubleCut_Enable="false" DoubleCut_OffsetLen="0" DoubleCut_FirstCutMode="1" Rotate180="255" Stop="18" Darkness="14" Number="1" bCutDataBase="false" bBatchCut="false" bNumberDataBase="false" NumberField="None" PageDirection="Portrait" PrintMode="1" PowerRFID="10" LengthRFID="0" RetryRFID="3" DrawMode="0" BackgroundColour_R="255" BackgroundColour_G="255" BackgroundColour_B="255" RibbonColour_R="0" RibbonColour_G="0" RibbonColour_B="0" colorNumber="0">
    <Layout Shape="2" AcrossType="Copied" PageDirection="Portrait" HorAcross="1" VerAcross="1" HorGap="0" VerGap="0" HorAcrossMode1="1" VerAcrossMode1="1" LabelMode="0" HorGapMode1="0" VerGapMode1="0" BottomMargin="0" RightMargin="0" />
    <Description>rasterezpl / GoLabel comparison — synthetic, examples/golabel_compare.py</Description>
    <UnitType>Mm</UnitType>
    <Dpi>{dpi}</Dpi>
  </Setup>
  <DriverName>Godex G500</DriverName>
  <PrinterModel>G500</PrinterModel>
  <PrinterLanguage>EZPL</PrinterLanguage>
  <USBName />
  <COMName />
  <CommunicationType>USB</CommunicationType>
  <NetworkIPAddress>0</NetworkIPAddress>
  <NetworkPort>9100</NetworkPort>
  <BaudRate>9600</BaudRate>
  <ComSettings>N81</ComSettings>
  <StandaloneDbSearchKey />
  <StandaloneDbEnable>false</StandaloneDbEnable>
</PrintJob>
"""

BOX = """      <GraphicShape xsi:type="WindowTextBox" Style="Cross" IsPrint="true" PageAlignment="None" Locked="false" bStroke="true" bFill="true" Direction="{angle}" X="{ax}" Y="{ay}" Alignment="{align}" AlignPointX="{ax}" AlignPointY="{ay}" FontScript="Default" FontCmd="{face},{pt}&#xD;&#xA;" LineSpacing="Single" LineHeight="{lh}" Multiple="10" DotBeforePara="0" DotAfterPara="0" FontWidth="24" BTrueType="true">
        <qHitOnCircumferance>false</qHitOnCircumferance>
        <Selected>false</Selected>
        <iBackground_color>4294967295</iBackground_color>
        <Id>{n}</Id>
        <ItemLabel>WindowTe{n:05d}</ItemLabel>
        <ObjectDrawMode>FW</ObjectDrawMode>
        <Name>W</Name>
        <bReplaceSpecialCharFromDB>false</bReplaceSpecialCharFromDB>
        <CharFilterRule>None</CharFilterRule>
        <LinkMode>OriginalData</LinkMode>
        <ReplaceInfoItems />
        <FormatType>None</FormatType>
        <P1 />
        <P2 />
        <P3 />
        <P4 />
        <DataField>None</DataField>
        <Prompt>None</Prompt>
        <BoundRectWidth>{w}</BoundRectWidth>
        <DispData>{text}</DispData>
        <bRemovePreZeroAndEmpty>false</bRemovePreZeroAndEmpty>
        <Data>{text}</Data>
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
        <SplitDataByAI>false</SplitDataByAI>
        <WrapMode>WordWrap</WrapMode>
        <TextSpace>0</TextSpace>
      </GraphicShape>
"""

FACE = "Microsoft YaHei UI"


def box(
    n: int,
    text: str,
    x: int,
    y: int,
    w: int,
    h: int,
    pt: int = 8,
    align: str = "Center",
    angle: str = "Angle0",
) -> str:
    """The anchor X/Y is the rectangle's top-left corner upright, its bottom-right corner turned round."""
    ax, ay = (x + w, y + h) if angle == "Angle180" else (x, y)
    lh = {8: 30, 9: 34, 10: 38}.get(pt, round(pt * 203 / 72 * 1.33))
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace("\n", "&#xD;&#xA;")
    return BOX.format(
        n=n, text=text, x=x, y=y, w=w, h=h, ax=ax, ay=ay, pt=pt, lh=lh, align=align, angle=angle, face=FACE
    )


def design(width_mm: float, length_mm: float, gap_mm: float, dpi: int) -> bytes:
    W, L = round(width_mm / 25.4 * dpi), round(length_mm / 25.4 * dpi)
    boxes = [
        box(1, "TL", 4, 4, 40, 30, align="Left"),
        box(2, "BR", W - 44, L - 34, 40, 30, align="Right"),
        box(3, "RASTEREZPL vs GOLABEL 9pt", 40, 6, W - 80, 36, pt=9),
        box(4, "left 8pt R3714 U32 Eth32/1", 10, 60, 330, 32, align="Left"),
        box(5, "two lines\nLineHeight 30", W - 320, 54, 300, 64),
        box(6, "this line is too long for its narrow box and will wrap", 10, 104, 220, 100, align="Left"),
        box(7, "TURNED ROUND back half 8pt", W - 400, L - 76, 380, 32, angle="Angle180"),
    ]
    xml = (
        HEAD
        + "".join(boxes)
        + TAIL.format(width=f"{width_mm:g}", length=f"{length_mm:g}", gap=f"{gap_mm:g}", dpi=dpi)
    )
    return xml.replace("\n", "\r\n").encode("utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("outdir")
    ap.add_argument("--width-mm", type=float, default=92)
    ap.add_argument("--length-mm", type=float, default=34)
    ap.add_argument("--gap-mm", type=float, default=2)
    ap.add_argument("--dpi", type=int, default=203)
    a = ap.parse_args()
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"compare-{a.width_mm:g}x{a.length_mm:g}.ezpx"
    f.write_bytes(design(a.width_mm, a.length_mm, a.gap_mm, a.dpi))
    print(f"wrote {f}: a {a.width_mm:g} × {a.length_mm:g} mm GoLabel design, 7 text boxes in {FACE}")
    print(
        f"GoLabel: open it and print one label.  Ours: rasterezpl import {f} --media <your media> -o {out / 'ours.ezpl'} --proof {out / 'ours.png'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
