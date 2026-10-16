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

from .media import Media
from .registry import Registry
from .stream import (
    blocks_outside_areas,
    count_labels,
    decode_block,
    matching_media,
    parse_blocks,
    parse_selection,
    retune,
    select_labels,
    tightest,
)
from .transport import deliver, wait_idle

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
    sent: bool | None = None  # after run(): True = delivered, False = the send failed, None = not attempted
    error: str | None = None  # the failed send's message

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
    fits = tightest([n for n in names if not blocks_outside_areas(data, reg.media[n])], reg.media)
    return fits, tightest([n for n in names if n not in fits], reg.media)


def holds(pr, names: list[str], reg: Registry) -> bool:
    """Whether a printer can take a job that fits one of `names`: its media is one of them, or the same stock at
    the same dpi as one of them (a variant that only differs in where blocks are allowed)."""
    return any(n == pr.media_name or pr.media.same_stock(reg.media[n]) for n in names)


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
        row["printers"] = [pr.name for pr in reg.printers.values() if holds(pr, names, reg)]
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
            if not holds(pr, names, reg):
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
            cands = [pr for pr in reg.printers.values() if holds(pr, names, reg)]
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
        row.printer = pr.name
        row.media = (
            pr.media_name
            if pr.media_name in names
            else next(n for n in names if pr.media.same_stock(reg.media[n]))
        )
        m = reg.media[row.media]  # the variant the file was written for (same page and dots as the printer's)
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


def wait_policy(settle: float = 0.0) -> str:
    """What happens between two files of a cart, as the plan states it."""
    if settle > 0:
        return f"between files: settle {settle:g} s, no status query"
    return "between files: poll the printer's status until it answers 00 (--settle N waits N s instead; no status = no wait)"


def format_plan(rows: list[PlanRow], bookends: bool = False, settle: float = 0.0) -> str:
    """The plan as a text table with a totals line (the CLI's output). ``count`` stays the real labels; with
    ``bookends`` every clean row shows the START / END pair it will carry; with two or more clean rows the wait
    between files (:func:`wait_policy`) is stated."""
    w = max([len(r.file) for r in rows] + [4])
    lines = [f"{'file':{w}s} {'labels':>7s}  {'printer':10s} labels"]
    for r in rows:
        state = f"REFUSED: {r.refused}" if r.refused else r.labels + (" +2 bookends" if bookends else "")
        lines.append(f"{r.file:{w}s} {r.count:7d}  {r.printer or '-':10s} {state}")
    ok = [r for r in rows if r.ok]
    by_p: dict[str, int] = {}
    for r in ok:
        by_p[r.printer or ""] = by_p.get(r.printer or "", 0) + r.count
    lines.append(
        f"{sum(r.count for r in ok)} labels in {len(ok)} files → "
        + (", ".join(f"{n} on {p}" for p, n in by_p.items()) or "nothing to send")
        + (" (+2 bookends per file)" if bookends and ok else "")
        + (f"; {len(rows) - len(ok)} file(s) refused" if len(ok) < len(rows) else "")
    )
    if len(ok) > 1:
        lines.append(wait_policy(settle))
    return "\n".join(lines)


def append_log(log: Path | None, entry: dict) -> None:
    """One JSON line per send, success or failure. Keys: ts, file, labels, count, printer, transport, note,
    bookends, ok, bytes_sent, and error on a failure — the keys of 0.3.1 … 0.13.0 unchanged, so every reader of
    the old lines still parses the new."""
    if log is None:
        return
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def send_logged(
    data: bytes,
    target: str,
    log: Path | None,
    *,
    file: str,
    labels: str,
    count: int,
    bookends: bool,
    printer: str,
    media: Media | None,
    offset_in: tuple[float, float] = (0.0, 0.0),
    darkness: int | None = None,
    speed: int | None = None,
) -> tuple[bool, str]:
    """Deliver one job (the heat retuned here when given) and log it either way — the one send path of
    ``print``, ``printcart`` and ``POST /api/print``. Returns (ok, note): the transport's note, or on failure the
    message with what the operator must know — nothing on the paper when 0 bytes went, else that labels ARE on
    the paper and the resume range is theirs to read off the roll."""
    heat = darkness is not None or speed is not None
    if heat:
        data = retune(data, darkness, speed)
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": file,
        "labels": labels,
        "count": count,
        "printer": printer,
        "transport": target,
        "note": "",
        "bookends": bookends,
        "ok": False,
        "bytes_sent": 0,
    }
    try:
        note, n = deliver(data, target, media, offset_in)
    except Exception as e:  # SendError (bytes known), a USB or lp failure (bytes unknown = 0)
        n = int(getattr(e, "bytes_sent", 0) or 0)
        msg = f"{e.__class__.__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
        entry["bytes_sent"] = n
        entry["error"] = msg
        entry["note"] = (
            "FAILED — nothing on the paper (0 bytes accepted)"
            if n == 0
            else f"FAILED after {n} bytes — labels ARE on the paper up to where the stream stopped"
        )
        append_log(log, entry)
        hint = (
            "; the job can be sent again as it is"
            if n == 0
            else "; do NOT resend the whole job — read the roll for the last label printed and resume with --labels N-M"
        )
        return False, f"FAILED: {msg}{hint}"
    if heat:
        note += f" (darkness {darkness if darkness is not None else 'file'}, speed {speed if speed is not None else 'file'})"
    entry.update(note=note, ok=True, bytes_sent=n)
    append_log(log, entry)
    return True, note


def bookended(m: Media, data: bytes, row: PlanRow, pr, ts: str | None = None) -> bytes:
    """A planned row's job stream (after its selection) with its START / END bookends around it — see
    :mod:`rasterezpl.bookends`. ``ts`` defaults to now, the print log's clock."""
    from .bookends import with_bookends

    return with_bookends(m, data, row.file, row.labels, row.total, row.count, pr.name, ts)


def run(
    root: Path,
    reg: Registry,
    rows: list[PlanRow],
    log: Path | None,
    dry_run: bool = False,
    to: str | None = None,
    bookends: bool = False,
    settle: float = 0.0,
) -> list[str]:
    """Send every row of a clean plan — nothing while any row is refused. One note per row; EVERY send, success
    or failure, is appended to ``log`` as a JSON line (:func:`append_log`). ``to`` overrides the transport for
    every row (a tcp://, lp: or file path — a test spool); the printer's media and offset still apply.
    ``bookends`` puts a START block before and an END block after every row's labels, in the same write
    (:mod:`rasterezpl.bookends`); ``count`` stays the labels. Between two rows the printer is waited for
    (:func:`rasterezpl.transport.wait_idle`: ``settle`` seconds, or its status until 00). A failed send stops
    the cart: the rows after it are not sent, and the note says so."""
    if dry_run:
        return ["dry run — " + ("+2 bookends per file, " if bookends else "") + "nothing sent"]
    if any(r.refused for r in rows):
        return ["REFUSED rows in the plan — nothing sent (fix the cart and run again)"]
    notes = []
    for i, r in enumerate(rows):
        pr = reg.printers[r.printer or ""]
        m = reg.media[r.media or pr.media_name]
        target = to or pr.transport
        if i:
            try:
                notes.append(wait_idle(target, settle))
            except RuntimeError as e:
                notes.append(f"FAILED: {e}; {len(rows) - i} file(s) not sent")
                r.sent, r.error = False, str(e)
                break
        data = select_labels(confine(root, r.file).read_bytes(), r.labels, m)
        if bookends:
            data = bookended(m, data, r, pr)
        ok, note = send_logged(
            data,
            target,
            log,
            file=r.file,
            labels=r.labels,
            count=r.count,
            bookends=bookends,
            printer=pr.name,
            media=pr.media,
            offset_in=pr.offset_in,
            darkness=pr.darkness,
            speed=pr.speed,
        )
        r.sent = ok
        notes.append(f"{r.file} labels {r.labels}{' +2 bookends' if bookends else ''} → {note}")
        if not ok:
            r.error = note
            if i + 1 < len(rows):
                notes.append(f"cart stopped: {len(rows) - i - 1} file(s) after {r.file} not sent")
            break
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
