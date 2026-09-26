"""The user's printers and media, in a YAML file — because registration offsets belong to one machine, not to a
code base or a job file.

Location: ``--registry PATH``, else ``$RASTEREZPL_PRINTERS``, else ``~/.config/rasterezpl/printers.yaml``.

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
      g500:
        transport: "usb:G500"
        media: godex-92x34-flags
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .media import Media
from .presets import PRESETS

DEFAULT_PATH = Path("~/.config/rasterezpl/printers.yaml").expanduser()


@dataclass(frozen=True)
class Printer:
    name: str
    transport: str
    media_name: str
    media: Media
    offset_in: tuple[float, float] = (0.0, 0.0)
    note: str = ""


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
    return DEFAULT_PATH if DEFAULT_PATH.exists() else None


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
        printers[name] = Printer(
            name=name,
            transport=str(d["transport"]),
            media_name=mname,
            media=media[mname],
            offset_in=(float(off[0]), float(off[1])),
            note=str(d.get("note", "")),
        )
    return Registry(path, media, printers)
