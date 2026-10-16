"""``rasterezpl`` — the command line.

rasterezpl printers                      the registry (printers, media, offsets)
rasterezpl media                         media presets + the registry's
rasterezpl usb                           USB printer-class devices on the bus
rasterezpl status  --printer P | --to T  ~S,CHECK (00 = ready)
rasterezpl text    --media M --font F --pt 5.4 "l1\\nl2" … | --rows F.csv [--template T] -o job.ezpl   text labels → job
rasterezpl image   --media M a.png b.png … -o job.ezpl                     bitmaps → job (one per area)
rasterezpl print   job.ezpl --printer P | --to T --media M [--labels 1,3,5-6 | --first N] [--offset x,y] [--status] [--dry-run] [--proof F.pdf|F.png]
rasterezpl ruler   --printer P | --to T --media M [job.ezpl [--labels …]]  calibration label, optionally over a label
rasterezpl decode  job.ezpl --media M -o page.png [--block N]              a job back to bitmaps (a proof)
rasterezpl printcart FILE[:LABELS] … [--root DIR] [--dry-run]              several files, one plan, one print
rasterezpl serve   --root DIR [--host 127.0.0.1] [--port 8123] [--open]   the browser front end (server.py)
rasterezpl demo    [DIR] [--no-open] [--no-serve]                         a playground: demo jobs + file-spool printers
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from .calibrate import ruler_job
from .compose import Spec, render
from .jobs import LOG_NAME, format_plan, parse_cart, plan, run
from .media import Media
from .registry import Registry, load
from .stream import (
    blocks_outside_areas,
    count_labels,
    decode_block,
    first_labels,
    job,
    match_media,
    matching_media,
    parse_blocks,
    retune,
    select_labels,
)
from .text import find_font
from .transport import list_usb_printers, send, status


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="rasterezpl",
        description="Print bitmaps to Godex-language (EZPL) label printers — Godex, Panduit TDP — with no driver.",
    )
    p.add_argument("--version", action="version", version=f"rasterezpl {__version__}")
    p.add_argument(
        "--registry",
        help="printers/media YAML (default $RASTEREZPL_PRINTERS or ~/.config/rasterezpl/printers.yaml)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("printers", help="list the registry's printers")
    sub.add_parser("media", help="list media (presets + registry)")
    sub.add_parser("usb", help="list USB printer-class devices (needs pyusb)")

    def target_args(q: argparse.ArgumentParser, need_media: bool = True) -> None:
        q.add_argument("--printer", help="a registry printer: its transport, media and offset")
        q.add_argument("--to", help="tcp://host[:9100] | usb:[serial|name|VID:PID] | lp:queue | a file path")
        if need_media:
            q.add_argument("--media", help="a media name (preset or registry) when not using --printer")

    q = sub.add_parser("status", help="~S,CHECK the printer (00 = ready)")
    target_args(q, need_media=False)

    q = sub.add_parser(
        "text", help="text labels → a job file (one label per positional argument, \\n = new line)"
    )
    q.add_argument("labels", nargs="*", help='label texts, e.g. "line 1\\nline 2" (or --rows)')
    q.add_argument(
        "--rows",
        help="a .csv / .tsv file (delimiter sniffed, header line names the columns): one label per row",
    )
    q.add_argument(
        "--template",
        default="",
        help="with --rows: the label text per row, {column} fields and \\n (default: every column a line)",
    )
    q.add_argument(
        "--no-header", action="store_true", help="with --rows: the file has no header (col1, col2, …)"
    )
    q.add_argument(
        "--layout",
        default="plain",
        help="a decoration around the text (rasterezpl.layouts), e.g. barcode-below",
    )
    q.add_argument(
        "--qr", help="what the QR / barcode layouts encode: {n}, {column} with --rows, or a fixed text"
    )
    q.add_argument("--media", required=True)
    q.add_argument(
        "--font", required=True, help="a face name known to rasterezpl (Arial, DejaVu Sans, …) or a .ttf path"
    )
    q.add_argument("--pt", type=float, required=True, help="font size in points")
    q.add_argument(
        "--area", type=int, default=None, help="print area index to use (default: fill areas in order)"
    )
    q.add_argument("--align", choices=["center", "left"], default="center")
    q.add_argument(
        "--fit", action="store_true", help="shrink the font until the widest line fits (never clip)"
    )
    q.add_argument("--stroke", type=int, default=0, help="extra dots of ink around every glyph (1–2: bolder)")
    q.add_argument("-o", "--out", required=True)

    q = sub.add_parser("image", help="bitmaps → a job file (each image fills one print area, in order)")
    q.add_argument("images", nargs="+")
    q.add_argument("--media", required=True)
    q.add_argument("-o", "--out", required=True)

    q = sub.add_parser("print", help="send a job")
    q.add_argument("file")
    target_args(q)
    q.add_argument("--labels", help="which labels, print-dialog style: 1 | 1-2 | 1,3,5-6 (1-based)")
    q.add_argument("--first", type=int, help="only the first N label blocks")
    q.add_argument("--offset", help="override the printer's registration, x,y inches (x ≥ 0 → ^R, y → ~Q)")
    q.add_argument("--status", action="store_true", help="~S,CHECK first; refuse to send unless 00")
    q.add_argument(
        "--dry-run",
        action="store_true",
        help="every guard and the summary (media match, blocks inside the areas, selection, heat, offset), send nothing",
    )
    q.add_argument(
        "--proof",
        help="also write the proof of every web row as it would be sent (after --labels/--first): .pdf = one page "
        "per row at true scale (print it on paper at 100 %% to lay over the real label), .png = the rows stacked",
    )
    q.add_argument(
        "--darkness", type=int, help="^H 0–19 for this print (default: the printer record's, else the file's)"
    )
    q.add_argument(
        "--speed", type=int, help="^S ips for this print (default: the printer record's, else the file's)"
    )
    q.add_argument(
        "--bookends",
        action="store_true",
        help="a START label before and an END label after the job, in the same send (black, hazard-striped, "
        "the job's name, labels and time; the END carries a CUT HERE band) — the paper then shows where the job "
        "begins and that it completed; two blocks, never counted as labels, never selectable",
    )

    q = sub.add_parser("ruler", help="print the calibration ruler (optionally over a job's first label)")
    q.add_argument("file", nargs="?")
    target_args(q)
    q.add_argument("--labels", help="with a file: which label to put under the ruler (default the first)")
    q.add_argument(
        "--font", default="Arial", help="face for the tick numbers (default Arial, then DejaVu Sans)"
    )

    q = sub.add_parser("decode", help="a job back to PNG pages (printer frame)")
    q.add_argument("file")
    q.add_argument("--media", help="media name; default: matched from the job's ^Q/^W header")
    q.add_argument("--block", type=int, help="only this block (1-based)")
    q.add_argument("-o", "--out", required=True, help="PNG path; several blocks → <stem>-<n>.png")

    q = sub.add_parser(
        "serve",
        help="the browser front end: job files under --root, the registry's printers, a cart, one print "
        "(http://127.0.0.1:8123/rasterezpl/)",
    )
    q.add_argument("--root", default=".", help="directory holding the .ezpl job files (served recursively)")
    q.add_argument(
        "--host", default="127.0.0.1", help="bind address (loopback by default — printers are physical)"
    )
    q.add_argument("--port", type=int, default=8123)
    q.add_argument("--log", help="print log, JSON lines (default <root>/.rasterezpl-printlog.jsonl)")
    q.add_argument("--open", action="store_true", help="open the page in the default browser")

    q = sub.add_parser(
        "demo",
        help="a playground with no printer: writes demo jobs (text labels, test patterns, the ruler) and a "
        "registry whose printers are FILE SPOOLS into DIR (default the temp dir), then serves the page on it",
    )
    q.add_argument("dir", nargs="?", help="the demo root (default <tempdir>/rasterezpl-demo)")
    q.add_argument("--port", type=int, default=8123)
    q.add_argument("--no-open", action="store_true", help="do not open the browser")
    q.add_argument("--no-serve", action="store_true", help="write the demo root only")

    q = sub.add_parser(
        "printcart",
        help="several job files in ONE plan and one print: FILE[:LABELS] … — the plan (file, labels, count, "
        "printer chosen by the file's media) is shown, then one job per file; nothing is sent while any entry is "
        "refused; every send is logged",
    )
    q.add_argument(
        "entry",
        nargs="+",
        help="a .ezpl job file (relative to --root), optionally :LABELS as in print --labels",
    )
    q.add_argument(
        "--root",
        default=".",
        metavar="DIR",
        help="the DIRECTORY the entries are relative to (default: the current one)",
    )
    q.add_argument("--dry-run", action="store_true", help="the plan only, send nothing")
    q.add_argument(
        "--bookends",
        action="store_true",
        help="a START and an END label around EVERY file's labels, in that file's send (see print --bookends)",
    )
    q.add_argument("--log", help="print log, JSON lines (default <root>/.rasterezpl-printlog.jsonl)")
    return p


def _media(reg: Registry, name: str | None, data: bytes | None = None) -> tuple[str, Media]:
    if name:
        if name not in reg.media:
            sys.exit(f"unknown media {name!r}; `rasterezpl media` lists them")
        return name, reg.media[name]
    if data is not None:
        return match_media(data, reg.media)
    sys.exit("--media or --printer is required")


def _resolve(reg: Registry, args, data: bytes | None = None):
    """(target, media_name, media, offset) from --printer / --to / --media."""
    if args.printer:
        if args.printer not in reg.printers:
            sys.exit(f"unknown printer {args.printer!r}; `rasterezpl printers` lists them")
        pr = reg.printers[args.printer]
        target = args.to or pr.transport
        return target, pr.media_name, pr.media, pr.offset_in, pr
    if not args.to:
        sys.exit("--printer or --to is required")
    if getattr(args, "media", None) is None and data is None:
        return args.to, "", None, (0.0, 0.0), None  # status, or a bare ruler: no media needed / known yet
    mname, m = _media(reg, getattr(args, "media", None), data)
    return args.to, mname, m, (0.0, 0.0), None


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.cmd == "demo":
        from .demo import demo

        return demo(Path(args.dir) if args.dir else None, args.port, not args.no_open, not args.no_serve)
    if args.cmd == "serve":
        from .server import serve

        return serve(Path(args.root), args.host, args.port, args.registry, args.log, open_browser=args.open)
    reg = load(args.registry)

    if args.cmd == "printcart":
        root = Path(args.root).resolve()
        if root.is_file():  # `--root some.ezpl` — the file was meant as an entry
            sys.exit(
                f"--root takes a directory, not a job file; did you mean:\n  rasterezpl printcart {args.root} "
                + " ".join(args.entry)
                + (" --dry-run" if args.dry_run else "")
            )
        if not root.is_dir():
            sys.exit(f"--root {args.root!r} is not a directory")
        try:
            rows = plan(root, reg, parse_cart(args.entry))
        except ValueError as e:
            sys.exit(str(e))
        print(format_plan(rows, bookends=args.bookends))
        log = Path(args.log).expanduser() if args.log else root / LOG_NAME
        for note in run(root, reg, rows, log, dry_run=args.dry_run, bookends=args.bookends):
            print(note)
        return 0 if all(r.ok for r in rows) else 2

    if args.cmd == "printers":
        if not reg.printers:
            print(
                f"no printers on record ({reg.path or 'no registry file'}); see `rasterezpl --help` for the YAML"
            )
        for pr in reg.printers.values():
            print(
                f"{pr.name:12s} {pr.transport:24s} {pr.media_name:28s} offset {pr.offset_in[0]:+.3f},{pr.offset_in[1]:+.3f} in  {pr.note}"
            )
        return 0
    if args.cmd == "media":
        for n, m in reg.media.items():
            print(
                f"{n:28s} {m.dpi} dpi  ^Q{round(m.length_mm)},{round(m.gap_mm)} ^W{round(m.width_mm)}  {len(m.areas_in)} area(s)  {m.name}"
            )
        return 0
    if args.cmd == "usb":
        for d in list_usb_printers():
            print(f"{d['ids']}  {d['manufacturer']} {d['product']}  serial {d['serial'] or '?'}")
        return 0
    if args.cmd == "status":
        target, *_ = _resolve(reg, args)
        st = status(target)
        print(f"{target}: {st or 'no reply'}  (00 = ready)")
        return 0 if st.startswith("00") else 1

    if args.cmd == "text":
        _media(reg, args.media)  # a clear error for an unknown media name
        if bool(args.labels) == bool(args.rows):
            sys.exit("text: give label texts or --rows FILE, not both, not neither")
        d: dict = {
            "media": args.media,
            "font": args.font,
            "pt": args.pt,
            "align": args.align,
            "fit": args.fit,
            "area": args.area,
            "stroke": args.stroke,
            "layout": args.layout,
            "qr": args.qr,
        }
        if args.rows:
            try:
                d["rows"] = Path(args.rows).read_text(encoding="utf-8-sig")
            except OSError as e:
                sys.exit(f"--rows {args.rows}: {e}")
            d["template"], d["header"] = args.template, not args.no_header
        else:
            d["labels"] = [
                re.sub(r"\\n", "\n", t) for t in args.labels
            ]  # a literal \n on the command line = a new line
            if args.qr:
                d["qrs"] = [args.qr] * len(d["labels"])
        try:
            spec = Spec.from_dict(d)
        except ValueError as e:
            sys.exit(str(e))
        try:
            data, m, _, texts = render(reg, spec)
        except ValueError as e:
            sys.exit(str(e))
        Path(args.out).write_bytes(data)
        print(f"wrote {args.out}: {len(texts)} labels in {count_labels(data)} blocks for {m.name}")
        return 0

    if args.cmd == "image":
        from PIL import Image

        _, m = _media(reg, args.media)
        imgs = [Image.open(p).convert("L") for p in args.images]
        per = m.labels_per_block
        for i, im in enumerate(imgs):
            _, _, aw, ah = m.area_px(i % per)
            if im.size != (aw, ah):
                sys.exit(f"{args.images[i]} is {im.size}, print area {i % per} is {(aw, ah)} dots")
        data = job(m, [imgs[i : i + per] for i in range(0, len(imgs), per)])
        Path(args.out).write_bytes(data)
        print(f"wrote {args.out}: {len(imgs)} labels in {count_labels(data)} blocks for {m.name}")
        return 0

    if args.cmd == "print":
        data = Path(args.file).read_bytes()
        target, mname, m, offset, pr = _resolve(reg, args, data)
        if pr is not None:
            try:
                names = matching_media(data, reg.media)
            except ValueError as e:
                sys.exit(str(e))
            if mname not in names:
                sys.exit(
                    f"REFUSED: {args.file} was written for {names or 'no known media'}, printer {pr.name} holds {mname}"
                )
        bad = blocks_outside_areas(data, m)
        if bad:
            bn, bx, by = bad[0]
            sys.exit(
                f"REFUSED: {args.file} has {len(bad)} block(s) outside {mname}'s print areas (first: block {bn} at "
                f"x {bx}, y {by}) — written for another frame or geometry of this stock; regenerate it"
            )
        total = count_labels(data)
        if args.labels:
            data = select_labels(data, args.labels, m)
        elif args.first:
            data = first_labels(data, args.first)
        n_labels = count_labels(data) * m.labels_per_block
        if args.bookends:  # after the selection, before the heat: never selectable, retuned like the labels
            from .bookends import with_bookends

            spec = args.labels or (f"1-{n_labels}" if n_labels > 1 else "1")  # --first / whole file = a range
            try:
                data = with_bookends(
                    m, data, args.file, spec, total * m.labels_per_block, n_labels, pr.name if pr else target
                )
            except ValueError as e:
                sys.exit(str(e))
        if args.offset:
            ox, oy = (float(v) for v in args.offset.split(","))
            offset = (ox, oy)
        dk = args.darkness if args.darkness is not None else (pr.darkness if pr else None)
        sp = args.speed if args.speed is not None else (pr.speed if pr else None)
        if dk is not None or sp is not None:
            data = retune(data, dk, sp)
            print(
                f"heat for this print: darkness {dk if dk is not None else 'file'}, speed {sp if sp is not None else 'file'}"
            )
        verb = "would send" if args.dry_run else "sending"
        print(
            f"{args.file}: {total} blocks × {m.labels_per_block} labels for {m.name}; {verb} {count_labels(data)} blocks"
            + (f" ({count_labels(data) - 2} + 2 bookends per file)" if args.bookends else "")
            + f" to {pr.name if pr else target}"
        )
        if args.proof:
            from .proof import proof_document

            try:
                n = proof_document(
                    m, data, args.proof, caption=f"{Path(args.file).name} → {pr.name if pr else target}"
                )
            except ValueError as e:
                sys.exit(str(e))
            print(
                f"proof: {args.proof} ({n} web rows, {m.dpi} dpi — the print, row by row, without the registration shift)"
            )
        if args.dry_run:
            if offset != (0.0, 0.0):
                print(f"registration {offset[0]:+.3f},{offset[1]:+.3f} in would go in front as ^R / ~Q")
            print("dry run — nothing sent")
            return 0
        if args.status:
            st = status(target)
            print(f"printer status: {st or 'no reply'} (00 = ready)")
            if not st.startswith("00"):
                print("not ready — nothing sent")
                return 2
        print(send(data, target, m, offset))
        if offset != (0.0, 0.0):
            print(f"registration {offset[0]:+.3f},{offset[1]:+.3f} in sent as ^R / ~Q")
        return 0

    if args.cmd == "ruler":
        fdata = Path(args.file).read_bytes() if args.file else None
        target, _, m, _, _ = _resolve(reg, args, fdata)
        if m is None:
            sys.exit("--media or --printer is required for the ruler")
        font = find_font(args.font, "DejaVu Sans")
        if font is None:
            sys.exit("no font for the tick numbers (Arial / DejaVu Sans)")
        over = None
        if fdata is not None:
            if args.labels:
                fdata = select_labels(fdata, args.labels, m)
            blocks = parse_blocks(fdata)
            if not blocks:
                sys.exit("no label block in the file")
            over = decode_block(blocks[0][1], m)
        print(send(ruler_job(m, font, over), target))
        print(
            "read the ruler (printer frame, no offset applied): x where the print-on edge on the x = 0 side cuts the "
            "scale → offset x; y at the print-on edge nearest the trailing edge minus the media's placement → offset y"
        )
        return 0

    if args.cmd == "decode":
        data = Path(args.file).read_bytes()
        _, m = _media(reg, args.media, data)
        blocks = parse_blocks(data)
        out = Path(args.out)
        picks = [args.block] if args.block else range(1, len(blocks) + 1)
        for bn in picks:
            page = decode_block(blocks[bn - 1][1], m)
            dest = out if len(picks) == 1 else out.with_name(f"{out.stem}-{bn}{out.suffix}")
            page.save(dest)
            print(f"wrote {dest}")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
