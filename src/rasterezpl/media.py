"""A label stock on an EZPL printer, and the setup commands it needs.

The stream grammar is the one Godex's own CUPS filter ``rastertoezpl`` 1.1.2 emits, which prints on every
EZPL model it lists (EZPL Programmer's Manual Rev. J, 2018-02-26, for the individual commands)::

    ^Q<length mm>,<gap mm>      label length + gap (gap label)            manual p.10
    ^W<width mm>                media width                                p.12
    ~R200                       no 180° rotation (x > head width)          p.32
    ^AT                         thermal transfer (^AD = direct thermal)
    ^S<n> ^H<nn> ^E<mm>         speed / darkness 00–19 / stop position     only when given
    ^P1                         one page                                   p.9
    ^L                          start of the label format                  p.6
    Q<x>,<y>,<bytes>,<rows>\\r<bytes×rows raw>\\r   pattern command: 1 bit = 1 black dot, MSB first   p.53
    E                           end of format = print                      p.46

Coordinate frame (measured on a Panduit TDP43ME, 2026-09-26): the origin is the TRAILING edge of the label,
x to the right as seen from the FRONT of the printer. A label drawn with y = 0 at the leading edge comes out
upside down at the tail end. ``Media.areas_in`` are therefore given in the PHYSICAL label frame (top = leading
edge, the frame of every label-design program's page and of the previews), and ``rotate180`` (default on)
turns each image into the printer frame before it goes out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EOL = b"\r"  # EZPL end of command (GDX_EOL in rastertoezpl)
OFFSET_MAX_DOTS = 100  # ~Q row offset range (manual p.32)
LEFT_MARGIN_MAX_DOTS = 399  # ^R range (manual p.10)


@dataclass(frozen=True)
class Media:
    """One label stock on one printer: the numbers the setup commands need.

    ``areas_in``: the print areas on ONE label, as (x, y, width, height) in inches from the label's top-left in
    the physical frame, in print order — one entry for a plain label, two for a two-across web, etc.
    """

    name: str
    dpi: int
    width_mm: float  # ^W — the media width across the head
    length_mm: float  # ^Q x — one label (or web row) along the feed
    gap_mm: float  # ^Q y — liner between labels
    thermal_transfer: bool = True  # ^AT (ribbon) vs ^AD
    speed: int | None = None  # ^S 2–7 ips; None = the printer's own setting
    darkness: int | None = None  # ^H 00–19; None = the printer's own setting
    stop_mm: int | None = None  # ^E 0–40; None = not sent
    rotate180: bool = True  # physical frame → printer frame (see the module docstring)
    areas_in: tuple[tuple[float, float, float, float], ...] = field(default_factory=tuple)

    def dots(self, inches: float) -> int:
        return round(inches * self.dpi)

    def area_px(self, k: int) -> tuple[int, int, int, int]:
        x, y, w, h = self.areas_in[k]
        return self.dots(x), self.dots(y), self.dots(w), self.dots(h)

    @property
    def width_px(self) -> int:
        return round(self.width_mm / 25.4 * self.dpi)

    @property
    def length_px(self) -> int:
        return round(self.length_mm / 25.4 * self.dpi)

    @property
    def labels_per_block(self) -> int:
        return max(1, len(self.areas_in))

    def printer_rect(self, k: int) -> tuple[int, int, int, int]:
        """Print area k as (x, y, w, h) in the PRINTER frame (rotated when ``rotate180``)."""
        ax, ay, aw, ah = self.area_px(k)
        if self.rotate180:
            ax, ay = self.width_px - (ax + aw), self.length_px - (ay + ah)
        return ax, ay, aw, ah

    def raw_page(self) -> Media:
        """The same media as one unrotated print area covering the whole page (printer frame) — for rulers."""
        return Media(
            name=self.name,
            dpi=self.dpi,
            width_mm=self.width_mm,
            length_mm=self.length_mm,
            gap_mm=self.gap_mm,
            thermal_transfer=self.thermal_transfer,
            speed=self.speed,
            darkness=self.darkness,
            stop_mm=self.stop_mm,
            rotate180=False,
            areas_in=((0.0, 0.0, self.width_px / self.dpi, self.length_px / self.dpi),),
        )


def setup_commands(m: Media) -> bytes:
    out = [
        f"^Q{round(m.length_mm)},{round(m.gap_mm)}".encode(),
        f"^W{round(m.width_mm)}".encode(),
        b"~R200",
        b"^AT" if m.thermal_transfer else b"^AD",
    ]
    if m.speed is not None:
        out.append(f"^S{m.speed}".encode())
    if m.darkness is not None:
        if not 0 <= m.darkness <= 19:
            raise ValueError(f"darkness {m.darkness} outside 0–19")
        out.append(f"^H{m.darkness:02d}".encode())
    if m.stop_mm is not None:
        out.append(f"^E{m.stop_mm}".encode())
    out.append(b"^P1")
    return EOL.join(out) + EOL


def offset_commands(m: Media, offset_in: tuple[float, float]) -> bytes:
    """A printer's registration, in dots, to prefix to a job — never written into a job file, because it belongs
    to one machine: x ≥ 0 → ``^R<n>`` (left margin: the print moves right as seen from the front, 0–399, manual
    p.10); y → ``~Q<±n>`` (row offset, + = down the feed, ±100, p.32).

    ``^XSET,OFFSET`` is deliberately not used: a Panduit TDP43ME (2010-era Godex firmware) ignored it. A negative x
    cannot be expressed by ^R — move the media's areas instead.
    """
    nx, ny = m.dots(offset_in[0]), m.dots(offset_in[1])
    if nx < 0:
        raise ValueError(f"x offset {offset_in[0]:+.3f} in is negative: ^R only moves the print right")
    if nx > LEFT_MARGIN_MAX_DOTS or abs(ny) > OFFSET_MAX_DOTS:
        raise ValueError(
            f"offset {nx:+d}/{ny:+d} dots outside ^R 0–{LEFT_MARGIN_MAX_DOTS} / ~Q ±{OFFSET_MAX_DOTS}"
        )
    out = bytearray()
    if nx:
        out += f"^R{nx}".encode() + EOL
    if ny:
        out += f"~Q{ny:+d}".encode() + EOL
    return bytes(out)
