"""The cross-OS fingerprint of the rendering: SHA-256 of every job the demo composes from the BUNDLED faces and of
a packed cart printed to the demo's file spool, written one `hash  name` line per file. Run on every OS of the
CI matrix (from the source tree) and from the packaged executable; the lines must be identical everywhere — that
is the one-rendering promise (the preview IS the print, on any machine) checked by machine, not by eye.

    python fingerprint.py OUT.txt -- CMD [ARG…]      e.g.  -- uv run rasterezpl   |   -- dist/rasterezpl/rasterezpl

Left out on purpose: the demo's text jobs (Arial / DejaVu Sans — the OS's own face, which differs by version),
the ruler (Arial too), and anything carrying a timestamp (bookends). Only `.ezpl` bytes are hashed: a PNG's
bytes depend on the zlib build, its pixels do not matter here."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

FILES = [
    "templates/layouts.ezpl",  # every layout in Inter / Inter Bold / JetBrains Mono / Bebas Neue, QR, Code 128, the mark
    "templates/clear-laminate.ezpl",
    "patterns/checker.ezpl",
    "patterns/stripes.ezpl",
    "patterns/gradient.ezpl",
    "spool/demo-panduit.ezpl",  # the packed cart below, registration prefix in front
]
CART = ["templates/layouts.ezpl:1-4,7", "patterns/checker.ezpl"]  # a partial selection: packing exercised


def main(argv: list[str]) -> int:
    if "--" not in argv or argv.index("--") != 1:
        print(__doc__)
        return 2
    out = Path(argv[0])
    cmd = argv[argv.index("--") + 1 :]
    root = Path(tempfile.mkdtemp(prefix="rasterezpl-fp-"))
    subprocess.run([*cmd, "demo", str(root), "--no-serve"], check=True)
    subprocess.run(
        [*cmd, "--registry", str(root / "printers.yaml"), "printcart", "--root", str(root), *CART], check=True
    )
    lines = []
    for rel in FILES:
        p = root / rel
        if not p.exists():
            print(f"missing {rel}", file=sys.stderr)
            return 1
        lines.append(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {rel}  {p.stat().st_size} bytes")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")  # LF on Windows too: the files are diffed
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
