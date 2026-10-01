"""Media presets — label stocks whose geometry has been verified on a printer. Add yours to the registry YAML
(``media:``) or here with a source for every number."""

from __future__ import annotations

from .media import Media

# Panduit S150X225VATY self-laminating cable labels, 1.5 × 2.25 in, 0.75 in print-on area, TWO across a 3.25 in web
# — the numbers of Panduit's own Easy-Mark format for the stock (page 3.25 × 2.375 in, 2 columns, margins 0.125 in
# left / 0.063 in top, repeat width 1.5 in = columns at a 1.5 in pitch with no gap), verified on a TDP43ME at
# 300 dpi on 2026-09-26: the two print-on areas span x ≈ 42 … 934 dots on a ruler print.
PANDUIT_S150X225VATY_2UP = Media(
    name="Panduit S150X225VATY (2 across, 300 dpi)",
    dpi=300,
    width_mm=3.25 * 25.4,
    length_mm=2.25 * 25.4,
    gap_mm=0.125 * 25.4,
    areas_in=(
        (0.125, 0.063, 1.5, 0.75),
        (0.125 + 1.5, 0.063, 1.5, 0.75),
    ),
    # the die-cut labels: 1.5 × 2.25 in each, the print-on area at the leading end, clear laminate below it
    labels_in=(
        (0.125, 0.0, 1.5, 2.25),
        (0.125 + 1.5, 0.0, 1.5, 2.25),
    ),
)

# the same stock on a 203 dpi head (a Godex G500 carrying Panduit labels): identical inches, coarser dots
PANDUIT_S150X225VATY_2UP_203 = Media(
    name="Panduit S150X225VATY (2 across, 203 dpi)",
    dpi=203,
    width_mm=PANDUIT_S150X225VATY_2UP.width_mm,
    length_mm=PANDUIT_S150X225VATY_2UP.length_mm,
    gap_mm=PANDUIT_S150X225VATY_2UP.gap_mm,
    areas_in=PANDUIT_S150X225VATY_2UP.areas_in,
    labels_in=PANDUIT_S150X225VATY_2UP.labels_in,
)

# the same stock with the WHOLE die-cut label as the print area — the clear laminate takes thermal-transfer ink
# just as the white block does, so a flat-stuck label (an asset tag, a badge) can carry a large QR or picture on
# the clear two thirds and its text on the white third. Same page, same dots, same printer: only where blocks
# may go differs. Not for a label that will be wrapped — the clear part becomes the wrap.
PANDUIT_S150X225VATY_2UP_FULL = Media(
    name="Panduit S150X225VATY (2 across, 300 dpi, full label incl. the clear laminate)",
    dpi=300,
    width_mm=PANDUIT_S150X225VATY_2UP.width_mm,
    length_mm=PANDUIT_S150X225VATY_2UP.length_mm,
    gap_mm=PANDUIT_S150X225VATY_2UP.gap_mm,
    areas_in=PANDUIT_S150X225VATY_2UP.labels_in,
    labels_in=PANDUIT_S150X225VATY_2UP.labels_in,
)

PRESETS: dict[str, Media] = {
    "panduit-s150x225vaty-2up": PANDUIT_S150X225VATY_2UP,
    "panduit-s150x225vaty-2up-203": PANDUIT_S150X225VATY_2UP_203,
    "panduit-s150x225vaty-2up-full": PANDUIT_S150X225VATY_2UP_FULL,
}
