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
)

PRESETS: dict[str, Media] = {
    "panduit-s150x225vaty-2up": PANDUIT_S150X225VATY_2UP,
}
