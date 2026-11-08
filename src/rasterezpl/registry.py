"""The user's printers and media, in a YAML file — because registration offsets belong to one machine, not to a
code base or a job file.

Location: ``--registry PATH``, else ``$RASTEREZPL_PRINTERS``, else the first of :func:`default_paths` that exists:
``%APPDATA%\\rasterezpl\\printers.yaml`` (Windows' place for a user's settings — looked at wherever ``APPDATA`` is
set), then ``~/.config/rasterezpl/printers.yaml`` (macOS, Linux, and a Windows setup that keeps it there).

.. code-block:: yaml

    media:
      godex-92x34-flags:            # any name; every key of Media except name
        dpi: 203
        width_mm: 92
        length_mm: 34
        gap_mm: 2
        speed: 3
        darkness: 14
        stop_mm: 18
        areas_in: [[0, 0, 3.622, 1.339]]   # inches, physical frame
    printers:
      tdp43me:
        transport: "usb:0123ABCD"   # tcp://192.168.1.50 | usb:<serial> | usb:<name part> | lp:queue
        media: panduit-s150x225vaty-2up      # a preset or a media above
        offset_in: [0.013, 0.03]    # from a ruler print; x ≥ 0 (^R), y ± (~Q)
        note: "ruler-read 2026-09-26"
        darkness: 17                # optional: ^H 0–19 for THIS head, applied at send time (files unchanged)
        speed: 2                    # optional: ^S ips, likewise
      g500:
        transport: "usb:G500"
        media: godex-92x34-flags
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .media import Media
from .presets import PRESETS

DEFAULT_PATH = Path("~/.config/rasterezpl/printers.yaml").expanduser()


def default_paths() -> list[Path]:
    """Where the registry is looked for when neither ``--registry`` nor ``$RASTEREZPL_PRINTERS`` names it, in
    order: ``%APPDATA%/rasterezpl/printers.yaml`` when ``APPDATA`` is set (Windows), then
    ``~/.config/rasterezpl/printers.yaml``. A new registry on Windows belongs in the first."""
    out = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        out.append(Path(appdata) / "rasterezpl" / "printers.yaml")
    out.append(DEFAULT_PATH)
    return out


def suggested_path() -> Path:
    """Where to put a registry on this machine: the idiomatic place among :func:`default_paths`."""
    return default_paths()[0]


@dataclass(frozen=True)
class Printer:
    name: str
    transport: str
    media_name: str
    media: Media
    offset_in: tuple[float, float] = (0.0, 0.0)
    note: str = ""
    darkness: int | None = None  # ^H 0–19 applied at send time to every job (this head prints light / dark)
    speed: int | None = None  # ^S ips applied at send time (slower = more heat per dot)
    # a cart for this printer is split into streams of at most this many bytes (0.24.0): the NetPS board took
    # 5 MB streams and cut a 19.8 MB one at 7.2 MB; None = one stream whatever the size
    max_stream_bytes: int | None = None
    # `settle:` (0.15.0, per_row_s / floor_s) is read for an old YAML's sake and IGNORED since 0.16.0: a cart is
    # one stream per printer, with no gap between files to wait for.
    settle_per_row_s: float | None = None
    settle_floor_s: float = 0.0


@dataclass(frozen=True)
class Registry:
    path: Path | None
    media: dict[str, Media]
    printers: dict[str, Printer]


def registry_path(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("RASTEREZPL_PRINTERS")
    if env:
        return Path(env).expanduser()
    return next((p for p in default_paths() if p.exists()), None)


def media_from_dict(name: str, d: dict) -> Media:
    areas = tuple(tuple(float(v) for v in a) for a in d.get("areas_in", ()))
    labels = tuple(tuple(float(v) for v in a) for a in d.get("labels_in", ()))
    for a in (*areas, *labels):
        if len(a) != 4:
            raise ValueError(f"media {name}: areas_in / labels_in entries are [x, y, w, h] inches, got {a}")
    return Media(
        name=str(d.get("name", name)),
        dpi=int(d["dpi"]),
        width_mm=float(d["width_mm"]),
        length_mm=float(d["length_mm"]),
        gap_mm=float(d["gap_mm"]),
        thermal_transfer=bool(d.get("thermal_transfer", True)),
        speed=None if d.get("speed") is None else int(d["speed"]),
        darkness=None if d.get("darkness") is None else int(d["darkness"]),
        stop_mm=None if d.get("stop_mm") is None else int(d["stop_mm"]),
        rotate180=bool(d.get("rotate180", True)),
        areas_in=areas,  # type: ignore[arg-type]
        labels_in=labels,  # type: ignore[arg-type]
    )


def load(explicit: str | None = None) -> Registry:
    """Presets plus the YAML file's media and printers (the file may be absent: presets only, no printers)."""
    path = registry_path(explicit)
    media: dict[str, Media] = dict(PRESETS)
    printers: dict[str, Printer] = {}
    if path is None:
        return Registry(None, media, printers)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for name, d in (doc.get("media") or {}).items():
        media[name] = media_from_dict(name, d)
    for name, d in (doc.get("printers") or {}).items():
        mname = d["media"]
        if mname not in media:
            raise ValueError(f"printer {name}: media {mname!r} is neither a preset nor defined in {path}")
        off = d.get("offset_in") or [0.0, 0.0]
        st = d.get("settle") or {}
        if not isinstance(st, dict) or set(st) - {"per_row_s", "floor_s"}:
            raise ValueError(
                f"printer {name}: settle is a block with per_row_s and floor_s (seconds), got {st!r}"
            )
        budget = None
        if d.get("max_stream_bytes") is not None:
            try:
                budget = parse_bytes(d["max_stream_bytes"])
            except ValueError as e:
                raise ValueError(f"printer {name}: max_stream_bytes: {e}") from e
        printers[name] = Printer(
            name=name,
            transport=str(d["transport"]),
            media_name=mname,
            media=media[mname],
            offset_in=(float(off[0]), float(off[1])),
            note=str(d.get("note", "")),
            darkness=None if d.get("darkness") is None else int(d["darkness"]),
            speed=None if d.get("speed") is None else int(d["speed"]),
            settle_per_row_s=None if st.get("per_row_s") is None else float(st["per_row_s"]),
            settle_floor_s=float(st.get("floor_s") or 0.0),
            max_stream_bytes=budget,
        )
    return Registry(path, media, printers)


def parse_bytes(v: object) -> int:
    """A byte budget as the YAML writes it: a number of bytes, or ``"5 MB"`` / ``"5MB"`` / ``"512 KB"`` (decimal)
    / ``"4 MiB"`` (binary)."""
    if isinstance(v, bool) or v is None:
        raise ValueError(f"not a byte size: {v!r}")
    if isinstance(v, (int, float)):
        n = int(v)
    else:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMG]i?B|B)?\s*", str(v), re.IGNORECASE)
        if not m:
            raise ValueError(f"not a byte size: {v!r} (write 5000000, '5 MB' or '4 MiB')")
        unit = (m.group(2) or "B").upper().replace("IB", "iB")
        mult = {"B": 1, "KB": 1000, "MB": 10**6, "GB": 10**9, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3}[
            unit
        ]
        n = round(float(m.group(1)) * mult)
    if n <= 0:
        raise ValueError(f"a byte size is positive, not {v!r}")
    return n


def format_bytes(n: int) -> str:
    """``5000000`` → ``"5 MB"``, ``4194304`` → ``"4 MiB"``, anything else ``"7,229,016 bytes"``."""
    for mult, unit in (
        (10**9, "GB"),
        (10**6, "MB"),
        (1000, "KB"),
        (1024**3, "GiB"),
        (1024**2, "MiB"),
        (1024, "KiB"),
    ):
        if n % mult == 0:
            return f"{n // mult} {unit}"
    return f"{n:,} bytes"
