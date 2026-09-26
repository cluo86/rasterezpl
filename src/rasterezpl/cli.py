"""``rasterezpl`` — the command line.

rasterezpl printers                      the registry (printers, media, offsets)
rasterezpl media                         media presets + the registry's
rasterezpl usb                           USB printer-class devices on the bus
rasterezpl status  --printer P | --to T  ~S,CHECK (00 = ready)
rasterezpl text    --media M --font F --pt 5.4 "l1\\nl2" … -o job.ezpl     text labels → job
rasterezpl image   --media M a.png b.png … -o job.ezpl                     bitmaps → job (one per area)
rasterezpl print   job.ezpl --printer P | --to T --media M [--labels 1,3,5-6 | --first N] [--offset x,y] [--status]
rasterezpl ruler   --printer P | --to T --media M [job.ezpl [--labels …]]  calibration label, optionally over a label
rasterezpl decode  job.ezpl --media M -o page.png [--block N]              a job back to bitmaps (a proof)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from . import __version__
from .calibrate import ruler_job
from .media import Media
from .registry import Registry, load
from .stream import (
    count_labels,
    decode_block,
    first_labels,
    job,
    match_media,
    parse_blocks,
    select_labels,
)
from .text import find_font, pt_to_px, render_text
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
    q.add_argument("labels", nargs="+", help='label texts, e.g. "line 1\\nline 2"')
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
    reg = load(args.registry)

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
        _, m = _media(reg, args.media)
        font = find_font(args.font)
        if font is None:
            sys.exit(
                f"font {args.font!r} not found on this machine (a substitute would print a different label)"
            )
        px = pt_to_px(args.pt, m.dpi)
        imgs = []
        for text in args.labels:
            k = args.area or 0
            _, _, aw, ah = m.area_px(k)
            lines = re.split(r"\\n|\n", text)  # a literal \n on the command line or a real newline
            imgs.append(render_text((aw, ah), lines, font, px, align=args.align, fit=args.fit))
        per = m.labels_per_block if args.area is None else 1
        rows_out: list[list] = []
        for i in range(0, len(imgs), per):
            imgs_k = imgs[i : i + per]
            if args.area is not None:
                row: list = [None] * m.labels_per_block
                row[args.area] = imgs_k[0]
                rows_out.append(row)
            else:
                rows_out.append(imgs_k)
        data = job(m, rows_out)
        Path(args.out).write_bytes(data)
        print(f"wrote {args.out}: {len(imgs)} labels in {count_labels(data)} blocks for {m.name}")
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
                fname, _ = match_media(data, reg.media)
            except ValueError as e:
                sys.exit(str(e))
            if fname != mname:
                sys.exit(f"REFUSED: {args.file} was written for {fname}, printer {pr.name} holds {mname}")
        total = count_labels(data)
        if args.labels:
            data = select_labels(data, args.labels, m)
        elif args.first:
            data = first_labels(data, args.first)
        if args.offset:
            ox, oy = (float(v) for v in args.offset.split(","))
            offset = (ox, oy)
        print(
            f"{args.file}: {total} blocks × {m.labels_per_block} labels for {m.name}; sending {count_labels(data)} blocks"
        )
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
