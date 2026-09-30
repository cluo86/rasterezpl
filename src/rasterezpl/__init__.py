"""rasterezpl — print any bitmap to Godex-language (EZPL) label printers, driverless, from macOS or Linux."""

__version__ = "0.5.0"

from .calibrate import ruler_job, ruler_page
from .compose import Spec, compose, import_pemx, parse_rows, parse_text
from .jobs import PlanRow, format_plan, list_files, plan, run
from .media import EOL, LEFT_MARGIN_MAX_DOTS, OFFSET_MAX_DOTS, Media, offset_commands, setup_commands
from .presets import PANDUIT_S150X225VATY_2UP, PANDUIT_S150X225VATY_2UP_203, PRESETS
from .proof import proof_image
from .server import make_server, serve
from .stream import (
    STRIP_ROWS,
    bitmap_rows,
    blocks_outside_areas,
    count_labels,
    decode_block,
    decode_job,
    first_labels,
    header_of,
    job,
    label_block,
    match_media,
    matching_media,
    parse_blocks,
    parse_selection,
    pattern_blocks,
    select_blocks,
    select_labels,
    serialise_block,
)
from .text import FONT_CANDIDATES, find_font, pt_to_px, render_text
from .transport import UsbPrinter, list_usb_printers, send, status, status_tcp, status_usb

__all__ = [
    "EOL",
    "LEFT_MARGIN_MAX_DOTS",
    "OFFSET_MAX_DOTS",
    "STRIP_ROWS",
    "FONT_CANDIDATES",
    "PANDUIT_S150X225VATY_2UP",
    "PANDUIT_S150X225VATY_2UP_203",
    "PRESETS",
    "Media",
    "PlanRow",
    "Spec",
    "UsbPrinter",
    "bitmap_rows",
    "compose",
    "blocks_outside_areas",
    "count_labels",
    "decode_block",
    "decode_job",
    "find_font",
    "format_plan",
    "first_labels",
    "header_of",
    "import_pemx",
    "job",
    "label_block",
    "list_files",
    "make_server",
    "plan",
    "serve",
    "list_usb_printers",
    "match_media",
    "matching_media",
    "offset_commands",
    "parse_blocks",
    "parse_rows",
    "parse_selection",
    "parse_text",
    "pattern_blocks",
    "proof_image",
    "pt_to_px",
    "render_text",
    "ruler_job",
    "ruler_page",
    "run",
    "select_blocks",
    "select_labels",
    "send",
    "serialise_block",
    "setup_commands",
    "status",
    "status_tcp",
    "status_usb",
]
