"""Print jobs as a layer of their own: which job files exist, what may go to which printer, the plan for a cart
of them, and the sending with a log — shared by the CLI (``rasterezpl printcart``) and the web front end.

The unit is a JOB FILE (``.ezpl``) under a ROOT directory. A cart is a list of ``{"file", "labels", "printer"}``
entries; :func:`plan` turns it into :class:`PlanRow` objects, one per entry, without sending anything, and
:func:`run` sends a clean plan — one job per file, through the printer's registry record (transport, media,
registration offset) — or nothing at all while any row is refused.

Guards, the same as ``rasterezpl print``:

* the file lies under the root and is a ``.ezpl``;
* it was written for a media some printer on record holds — the printer is chosen by that media, or named;
* every block lies inside that media's print areas (a job written for another frame or geometry is refused);
* the label selection is well formed and within the file.
"""

from __future__ import annotations

import io
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .registry import Registry
from .stream import (
    blocks_outside_areas,
    count_labels,
    decode_block,
    matching_media,
    parse_blocks,
    parse_selection,
    select_labels,
)
from .transport import send

SPEC_RE = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")
LOG_NAME = ".rasterezpl-printlog.jsonl"


@dataclass
class PlanRow:
    """One cart entry, planned.

    ``file``: the job file, relative to the root, as given.  ``labels``: the print-dialog selection (filled in
    as the whole file when the entry gave none).  ``count`` / ``total``: labels selected / in the file.
    ``printer`` / ``media``: the registry printer that will take it and the media it holds.  ``refused``: why
    the row cannot go, else ``None``.
    """

    file: str
    labels: str
    count: int
    total: int
    printer: str | None
    media: str | None
    refused: str | None

    @property
    def ok(self) -> bool:
        return self.refused is None


def confine(root: Path, rel: str) -> Path:
    """The job file for a request path — refusing anything outside the root or not a ``.ezpl``."""
    p = (root / rel.lstrip("/")).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"{rel!r} is outside the served root")
    if p.suffix.lower() != ".ezpl":
        raise ValueError(f"{rel!r} is not a .ezpl job file")
    return p


def parse_cart(entries: list[str]) -> list[dict]:
    """Command-line cart entries ``FILE:LABELS`` (``LABELS`` optional — the whole file) to plan input; repeated
    files are kept as separate rows in the order given."""
    out = []
    for s in entries:
        file, sep, spec = s.rpartition(":")
        if not sep or not file or (spec and not SPEC_RE.match(spec)):
            file, spec = s, ""
            if ":" in s and not Path(s).exists():
                raise ValueError(f"cart entry {s!r}: expected FILE or FILE:LABELS, e.g. a/b.ezpl:1-4,7")
        out.append({"file": file, "labels": spec})
    return out


def written_for(data: bytes, reg: Registry) -> tuple[list[str], list[str]]:
    """(fits, header_only): media the job matches by header AND whose print areas contain every block, and
    media that match the header only — the same stock at another dpi places its blocks elsewhere, so a job
    for the 203 dpi variant is not a job for the 300 dpi one."""
    names = matching_media(data, reg.media)
    fits = [n for n in names if not blocks_outside_areas(data, reg.media[n])]
    return fits, [n for n in names if n not in fits]


def list_files(root: Path, reg: Registry) -> list[dict]:
    """Every ``.ezpl`` under the root: size, blocks, labels, the media it matches and the printers holding it."""
    out = []
    for p in sorted(root.rglob("*.ezpl")):
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        row: dict = {"file": rel, "bytes": len(data), "blocks": 0, "labels": 0, "media": [], "printers": []}
        try:
            names, header_only = written_for(data, reg)
        except ValueError as e:
            row["error"] = str(e)
            out.append(row)
            continue
        per = max((reg.media[n].labels_per_block for n in names + header_only), default=1)
        row["blocks"] = count_labels(data)
        row["labels"] = row["blocks"] * per
        row["media"] = names
        row["printers"] = [pr.name for pr in reg.printers.values() if pr.media_name in names]
        if not names and header_only:
            row["error"] = (
                f"written for {header_only} at a geometry no media here has (blocks outside the print areas)"
            )
        out.append(row)
    return out


def plan(root: Path, reg: Registry, jobs: list[dict]) -> list[PlanRow]:
    """One :class:`PlanRow` per cart entry. Pure — nothing is sent."""
    rows: list[PlanRow] = []
    for j in jobs:
        file = str(j.get("file", ""))
        spec = str(j.get("labels", "") or "").replace(" ", "")
        given = j.get("printer") or None
        row = PlanRow(file, spec, 0, 0, given, None, None)
        rows.append(row)
        try:
            p = confine(root, file)
        except ValueError as e:
            row.refused = str(e)
            continue
        if not p.exists():
            row.refused = "missing — regenerate its family"
            continue
        data = p.read_bytes()
        try:
            names, header_only = written_for(data, reg)
        except ValueError as e:
            row.refused = str(e)
            continue
        if given:
            pr = reg.printers.get(given)
            if pr is None:
                row.refused = f"unknown printer {given!r}"
                continue
            if pr.media_name not in names:
                other = (
                    f" (same stock at another dpi: this file fits {names})"
                    if pr.media_name in header_only
                    else ""
                )
                row.refused = (
                    f"{file} was written for {names or header_only or 'no known media'}, {pr.name} holds "
                    f"{pr.media_name}{other}"
                )
                continue
        else:
            cands = [pr for pr in reg.printers.values() if pr.media_name in names]
            if not cands:
                held = [
                    f"{pr.name} holds {pr.media_name}"
                    for pr in reg.printers.values()
                    if pr.media_name in header_only
                ]
                row.refused = (
                    f"no printer on record holds {names or header_only or 'the media this file was written for'}"
                    + (
                        f" — {'; '.join(held)}: the same stock at another dpi, the blocks would land elsewhere"
                        if held
                        else ""
                    )
                )
                continue
            if len(cands) > 1:
                row.refused = (
                    f"choose a printer: {', '.join(c.name for c in cands)} all hold this file's media"
                )
                continue
            pr = cands[0]
        row.printer, row.media = pr.name, pr.media_name
        m = pr.media
        row.total = count_labels(data) * m.labels_per_block
        if not spec:
            spec = f"1-{row.total}" if row.total > 1 else "1"
            row.labels = spec
        if not SPEC_RE.match(spec):
            row.refused = f"labels {spec!r}: use the print-dialog form 1 | 1-2 | 1,3,5-6"
            continue
        try:
            picked = parse_selection(spec, row.total)
        except ValueError as e:
            row.refused = str(e)
            continue
        row.count = len(picked)
        if not picked:
            row.refused = "no label selected"
            continue
        bad = blocks_outside_areas(data, m)
        if bad:
            bn, bx, by = bad[0]
            row.refused = (
                f"{len(bad)} block(s) outside {pr.media_name}'s print areas (first: block {bn} at x {bx}, y {by})"
                " — written for another frame or geometry of this stock; regenerate it"
            )
    return rows


def format_plan(rows: list[PlanRow]) -> str:
    """The plan as a text table with a totals line (the CLI's output)."""
    w = max([len(r.file) for r in rows] + [4])
    lines = [f"{'file':{w}s} {'labels':>7s}  {'printer':10s} labels"]
    for r in rows:
        state = f"REFUSED: {r.refused}" if r.refused else r.labels
        lines.append(f"{r.file:{w}s} {r.count:7d}  {r.printer or '-':10s} {state}")
    ok = [r for r in rows if r.ok]
    by_p: dict[str, int] = {}
    for r in ok:
        by_p[r.printer or ""] = by_p.get(r.printer or "", 0) + r.count
    lines.append(
        f"{sum(r.count for r in ok)} labels in {len(ok)} files → "
        + (", ".join(f"{n} on {p}" for p, n in by_p.items()) or "nothing to send")
        + (f"; {len(rows) - len(ok)} file(s) refused" if len(ok) < len(rows) else "")
    )
    return "\n".join(lines)


def run(
    root: Path,
    reg: Registry,
    rows: list[PlanRow],
    log: Path | None,
    dry_run: bool = False,
    to: str | None = None,
) -> list[str]:
    """Send every row of a clean plan — nothing while any row is refused. One note per row; every real send is
    appended to ``log`` as a JSON line (ts, file, labels, count, printer, transport, note). ``to`` overrides the
    transport for every row (a tcp://, lp: or file path — a test spool); the printer's media and offset still
    apply."""
    if dry_run:
        return ["dry run — nothing sent"]
    if any(r.refused for r in rows):
        return ["REFUSED rows in the plan — nothing sent (fix the cart and run again)"]
    notes = []
    for r in rows:
        pr = reg.printers[r.printer or ""]
        data = select_labels(confine(root, r.file).read_bytes(), r.labels, pr.media)
        target = to or pr.transport
        note = send(data, target, pr.media, pr.offset_in)
        notes.append(f"{r.file} labels {r.labels} → {note}")
        if log is not None:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "file": r.file,
                "labels": r.labels,
                "count": r.count,
                "printer": pr.name,
                "transport": target,
                "note": note,
            }
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return notes


def proof_png(root: Path, reg: Registry, file: str, label: int = 1) -> bytes:
    """The proof of the web row holding ``label`` (1-based), as PNG bytes (see :func:`rasterezpl.proof_image`)."""
    from .proof import proof_image

    p = confine(root, file)
    data = p.read_bytes()
    names = matching_media(data, reg.media)
    if not names:
        raise ValueError(f"{file} matches no known media")
    m = reg.media[names[0]]
    blocks = parse_blocks(data)
    bn = max(1, (label - 1) // m.labels_per_block + 1)
    if bn > len(blocks):
        raise ValueError(f"label {label} past the file's end")
    img = proof_image(m, decode_block(blocks[bn - 1][1], m), caption=f"{file} — web row {bn} ({m.name})")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def read_log(log: Path | None, n: int = 50) -> list[dict]:
    """The last ``n`` entries of the print log (newest last)."""
    if log is None or not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines[-n:] if ln.strip()]
