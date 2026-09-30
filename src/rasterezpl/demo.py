"""``rasterezpl demo`` — a playground with no printer: a demo root of job files (text labels, test patterns) and
a registry whose two "printers" are FILE SPOOLS, then the browser front end on it.

    rasterezpl demo [DIR] [--port 8123] [--no-open] [--no-serve]

Everything the real thing does happens here — the plan, the guards, the proof, the send, the log — except the
last inch: a print lands in ``DIR/spool/<printer>.ezpl`` instead of a head. Decode that spool
(``rasterezpl decode``) or open its proof on the page and you see exactly what a printer would have printed.
Rebuilding the demo root is idempotent; a spool file is overwritten by the next print to it.
"""

from __future__ import annotations

from pathlib import Path

from .layouts import LAYOUTS, render_label
from .presets import PANDUIT_S150X225VATY_2UP
from .registry import load
from .stream import job
from .text import bundled_fonts, find_font, pt_to_px, render_text

DEMO_DIRNAME = "rasterezpl-demo"

REGISTRY_YAML = """# rasterezpl demo registry — two "printers" that are FILE SPOOLS: a print lands in spool/<name>.ezpl.
# Swap a transport for usb:<serial> or tcp://host and the same page drives a real head.
media:
  demo-92x34-flags:            # a 92 x 34 mm flag label at 203 dpi, one print area over the whole label
    dpi: 203
    width_mm: 92
    length_mm: 34
    gap_mm: 2
    areas_in: [[0, 0, 3.622, 1.339]]
    labels_in: [[0, 0, 3.622, 1.339]]
printers:
  demo-panduit:
    transport: "{root}/spool/demo-panduit.ezpl"
    media: panduit-s150x225vaty-2up      # preset: 1.5 x 2.25 in self-laminating, two across, 300 dpi
    offset_in: [0.01, 0.0]               # a registration offset, so you can see ^R / ~Q go in front of a job
    note: "file spool — decode it or open its proof"
  demo-godex:
    transport: "{root}/spool/demo-godex.ezpl"
    media: demo-92x34-flags
    offset_in: [0.0, 0.0]
    note: "file spool"
"""

README_TXT = """rasterezpl demo root
====================

Job files (.ezpl) as a printer would receive them, a registry whose printers are file spools, and the print
log the front end keeps. Try, from this directory:

  rasterezpl --registry printers.yaml printers
  rasterezpl --registry printers.yaml print text/welcome.ezpl --printer demo-panduit --labels 1-2
  rasterezpl --registry printers.yaml decode spool/demo-panduit.ezpl -o spool/demo-panduit.png
  rasterezpl --registry printers.yaml printcart text/welcome.ezpl:1,4 patterns/checker.ezpl --dry-run
  rasterezpl --registry printers.yaml serve --root . --open        # the page: proofs, cart, compose

What is here
  text/welcome.ezpl        four text labels on the Panduit two-across stock (Arial or DejaVu Sans)
  text/flags.ezpl          three tags on the 92 x 34 mm flag media
  templates/layouts.ezpl   every layout (framed, banner, sidebar, corners, ticket, plain) with its sample text,
                           set in the bundled faces Inter, Inter Bold, JetBrains Mono, Bebas Neue
  patterns/checker.ezpl    a 16-dot checkerboard filling both print areas — dot-exact geometry check
  patterns/stripes.ezpl    diagonal stripes, 1 dot wide, 8 apart — a head/ribbon check
  patterns/gradient.ezpl   an ordered-dither gradient — darkness and speed settings
  (the registration ruler is not a job file — print it with:
   rasterezpl --registry printers.yaml ruler --printer demo-panduit, then decode the spool)
  spool/                   where prints land; .rasterezpl-printlog.jsonl is the log

Everything a real print does happens here except the last inch: the bytes go to a file, not a head.
The two printers here are file spools ON PURPOSE — the playground cannot reach a real printer, and the page's
"check" on them reports the spool, not a USB device.

Real printers: serve this same root with YOUR registry (the one `rasterezpl printers` lists) instead of the
demo's, and the plan routes these jobs to the heads that hold their media — a print from there is real output:

  rasterezpl serve --root <this directory> --open           # ~/.config/rasterezpl/printers.yaml
  rasterezpl serve --root <this directory> --registry my-printers.yaml --open
"""


def _checker(w: int, h: int, cell: int = 16):
    from PIL import Image

    img = Image.new("L", (w, h), 255)
    px = img.load()
    assert px is not None
    for y in range(h):
        for x in range(w):
            if ((x // cell) + (y // cell)) % 2 == 0:
                px[x, y] = 0
    return img


def _stripes(w: int, h: int, period: int = 8):
    from PIL import Image

    img = Image.new("L", (w, h), 255)
    px = img.load()
    assert px is not None
    for y in range(h):
        for x in range(w):
            if (x + y) % period == 0:
                px[x, y] = 0
    return img


def _gradient(w: int, h: int):
    """Left white → right black, ordered dither (a 4×4 Bayer matrix): what the head makes of grey."""
    from PIL import Image

    bayer = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]
    img = Image.new("L", (w, h), 255)
    px = img.load()
    assert px is not None
    for y in range(h):
        for x in range(w):
            level = x * 16 // max(1, w)  # 0 … 15 across the width
            if level > bayer[y % 4][x % 4]:
                px[x, y] = 0
    return img


def build(root: Path) -> dict:
    """Write the demo root. Returns what was written and what was skipped (no font → no text jobs)."""
    root = root.resolve()
    for d in ("text", "patterns", "templates", "spool"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "printers.yaml").write_text(REGISTRY_YAML.format(root=root.as_posix()), encoding="utf-8")
    (root / "README.txt").write_text(README_TXT, encoding="utf-8")
    reg = load(str(root / "printers.yaml"))
    pand, flags = PANDUIT_S150X225VATY_2UP, reg.media["demo-92x34-flags"]
    written: list[str] = []
    skipped: list[str] = []
    font = find_font("Arial", "DejaVu Sans")
    if font is None:
        skipped.append("text/welcome.ezpl, text/flags.ezpl (no Arial / DejaVu Sans on this machine)")
    else:
        _, _, aw, ah = pand.area_px(0)
        px = pt_to_px(5.4, pand.dpi)
        texts = [
            "rasterezpl\ndemo label 1\nprinted from the browser\nor the command line",
            "rasterezpl\ndemo label 2\ntwo labels across\none web row",
            "rasterezpl\ndemo label 3\nwhat you see in the proof\nis what the head gets",
            "rasterezpl\ndemo label 4\nno driver, no vendor tool\njust EZPL",
        ]
        imgs = [render_text((aw, ah), t.split("\n"), font, px) for t in texts]
        (root / "text" / "welcome.ezpl").write_bytes(job(pand, [imgs[0:2], imgs[2:4]]))
        written.append("text/welcome.ezpl")
        _, _, fw, fh = flags.area_px(0)
        fpx = pt_to_px(8, flags.dpi)
        tags = [
            "TAG #1\nA rack 1 U2\nB rack 9 U31",
            "TAG #2\nA rack 1 U3\nB rack 9 U31",
            "TAG #3\nA rack 1 U5\nB rack 9 U36",
        ]
        (root / "text" / "flags.ezpl").write_bytes(
            job(flags, [[render_text((fw, fh), t.split("\n"), font, fpx)] for t in tags])
        )
        written.append("text/flags.ezpl")
    # templates: every layout with its sample text, in the bundled faces (they render the same everywhere)
    faces = bundled_fonts()
    face_for = {
        "plain": "Inter",
        "framed": "Inter",
        "banner": "Bebas Neue",
        "sidebar": "JetBrains Mono",
        "corners": "Inter Bold",
        "ticket": "Bebas Neue",
    }
    _, _, aw, ah = pand.area_px(0)
    tpl_imgs = []
    for lay in LAYOUTS.values():
        fpath = faces[face_for.get(lay.name, "Inter")]
        big = lay.name in ("banner", "ticket")
        tpl_imgs.append(
            render_label(
                aw,
                ah,
                lay.sample.split("\n"),
                fpath,
                pt_to_px(7 if big else 5.4, pand.dpi),
                lay.name,
                align="left" if lay.name == "sidebar" else "center",
            )
        )
    (root / "templates" / "layouts.ezpl").write_bytes(
        job(pand, [tpl_imgs[i : i + 2] for i in range(0, len(tpl_imgs), 2)])
    )
    written.append("templates/layouts.ezpl")
    (root / "patterns" / "checker.ezpl").write_bytes(job(pand, [[_checker(aw, ah), _checker(aw, ah, 8)]]))
    (root / "patterns" / "stripes.ezpl").write_bytes(job(pand, [[_stripes(aw, ah), _stripes(aw, ah, 4)]]))
    (root / "patterns" / "gradient.ezpl").write_bytes(job(pand, [[_gradient(aw, ah), _gradient(aw, ah)]]))
    written += ["patterns/checker.ezpl", "patterns/stripes.ezpl", "patterns/gradient.ezpl"]
    return {"root": root, "registry": root / "printers.yaml", "written": written, "skipped": skipped}


def demo(
    root: Path | None = None, port: int = 8123, open_browser: bool = True, serve_page: bool = True
) -> int:
    import tempfile

    root = root or Path(tempfile.gettempdir()) / DEMO_DIRNAME
    info = build(root)
    print(f"demo root: {info['root']}")
    for w in info["written"]:
        print(f"  wrote {w}")
    for s in info["skipped"]:
        print(f"  skipped {s}")
    print(f"  registry {info['registry'].name}: printers demo-panduit, demo-godex → file spools under spool/")
    print(
        "  (no real printer is reachable from the demo; to print these jobs for real, serve this root with your"
    )
    print("   own registry: rasterezpl serve --root <dir> --open — see README.txt there)")
    if not serve_page:
        return 0
    from .server import serve

    return serve(root, "127.0.0.1", port, str(info["registry"]), None, open_browser=open_browser)
