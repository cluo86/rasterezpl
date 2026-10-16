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
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .media import Media, offset_commands
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
    serialise_block,
    tightest,
)
from .transport import deliver

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
    rows: int = 0  # web rows (label blocks) the selection sends — a partly selected row counts
    sent: bool | None = None  # after run(): True = delivered, False = cut or not sent, None = not attempted
    error: str | None = None  # why it was not delivered
    stream: dict | None = None  # after run(): {"id", "offset", "length"} — where this file sits in its stream

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
        row.rows = len({(k - 1) // m.labels_per_block for k in picked})
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


def format_plan(rows: list[PlanRow], bookends: bool = False) -> str:
    """The plan as a text table with a totals line (the CLI's output). ``count`` stays the real labels; with
    ``bookends`` every clean row shows the START / END pair it will carry. The totals say how many streams go:
    one per printer, the files back to back inside it."""
    w = max([len(r.file) for r in rows] + [4])
    lines = [f"{'file':{w}s} {'labels':>7s}  {'printer':10s} labels"]
    ok = [r for r in rows if r.ok]
    for r in rows:
        state = f"REFUSED: {r.refused}" if r.refused else r.labels + (" +2 bookends" if bookends else "")
        lines.append(f"{r.file:{w}s} {r.count:7d}  {r.printer or '-':10s} {state}")
    by_p: dict[str, list[PlanRow]] = {}
    for r in ok:
        by_p.setdefault(r.printer or "", []).append(r)
    lines.append(
        f"{sum(r.count for r in ok)} labels in {len(ok)} files → "
        + (
            ", ".join(
                f"{sum(r.count for r in rs)} on {p} ({len(rs)} file{'s' if len(rs) != 1 else ''}, one stream)"
                for p, rs in by_p.items()
            )
            or "nothing to send"
        )
        + (" (+2 bookends per file)" if bookends and ok else "")
        + (f"; {len(rows) - len(ok)} file(s) refused" if len(ok) < len(rows) else "")
    )
    return "\n".join(lines)


def append_log(log: Path | None, entry: dict) -> None:
    """One JSON line per file, success or failure. Keys: ts, file, labels, count, printer, transport, note,
    bookends, ok, bytes_sent, stream {id, offset, length}, and error on a failure — the keys of 0.3.1 … 0.14.0
    unchanged, so every reader of the old lines still parses the new."""
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
    """Deliver ONE job (the heat retuned here when given) and log it either way — the send path of ``print``.
    Returns (ok, note): the transport's note, or on failure the message with what the operator must know —
    nothing on the paper when 0 bytes went, else that labels ARE on the paper and the resume range is theirs to
    read off the roll."""
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


@dataclass
class Part:
    """One file's place in a stream: its blocks (after the selection, the bookends, and for the first part the
    heat retune), ``offset`` = its first byte in the stream as sent (the registration prefix counted),
    ``rows`` = its blocks, bookends included."""

    row: PlanRow
    data: bytes
    offset: int = 0
    rows: int = 0
    per: int = 1  # labels per web row of the media (to name the labels of a cut row)

    @property
    def length(self) -> int:
        return len(self.data)

    @property
    def end(self) -> int:
        return self.offset + len(self.data)


@dataclass
class Stream:
    """ONE job per printer: the registration prefix once, then every file of the cart for that printer back to
    back, each with its bookends when asked — sent over one connection, so no file's first byte ever meets a
    board that is still printing the previous file (the NetPS board resets such a connection; inside one
    connection it simply takes bytes as its buffer drains and the chunked send rides the stall)."""

    printer: str
    media: Media
    parts: list[Part]
    prefix: bytes = b""
    id: str = ""

    @property
    def data(self) -> bytes:
        return self.prefix + b"".join(p.data for p in self.parts)

    @property
    def body(self) -> bytes:
        """The stream without the registration prefix — what a proof or a media guard reads."""
        return b"".join(p.data for p in self.parts)

    @property
    def blocks(self) -> int:
        return sum(p.rows for p in self.parts)

    @property
    def length(self) -> int:
        return len(self.prefix) + sum(p.length for p in self.parts)


def compose_streams(root: Path, reg: Registry, rows: list[PlanRow], bookends: bool = False) -> list[Stream]:
    """The clean plan as streams, one per printer in order of first appearance; the files in cart order inside.
    Every part is built from its own file (selection, bookends); the printer's darkness / speed are retuned
    ONCE, on the first part's setup, as a single job's would be; the prefix is the printer's registration."""
    by: dict[str, list[PlanRow]] = {}
    for r in rows:
        by.setdefault(r.printer or "", []).append(r)
    out = []
    for name, rs in by.items():
        pr = reg.printers[name]
        parts = []
        for r in rs:
            m = reg.media[r.media or pr.media_name]
            if not m.same_stock(pr.media):  # the plan refuses this already; a stream never mixes stocks
                raise ValueError(
                    f"{r.file}: media {r.media} is not the stock {pr.name} holds ({pr.media_name})"
                )
            data = select_labels(confine(root, r.file).read_bytes(), r.labels, m)
            if bookends:
                data = bookended(m, data, r, pr)
            parts.append(Part(r, data, per=m.labels_per_block))
        if pr.darkness is not None or pr.speed is not None:
            parts[0].data = retune(parts[0].data, pr.darkness, pr.speed)
        prefix = offset_commands(pr.media, pr.offset_in) if pr.offset_in != (0.0, 0.0) else b""
        off = len(prefix)
        for p in parts:
            p.offset, p.rows = off, count_labels(p.data)
            off += p.length
        out.append(Stream(name, pr.media, parts, prefix, uuid.uuid4().hex[:8]))
    return out


def describe_stream(st: Stream, bookends: bool) -> str:
    """One line for the plan / dry run: files, blocks, bookends, bytes, and each file's rows."""
    n = len(st.parts)
    files = " · ".join(
        f"{p.row.file} labels {p.row.labels} ({p.rows - 2 if bookends else p.rows} rows)" for p in st.parts
    )
    return (
        f"stream {st.id} → {st.printer}: {n} file{'s' if n != 1 else ''}, {st.blocks} blocks"
        + (f" ({2 * n} bookends)" if bookends else "")
        + f", {st.length:,} bytes — {files}"
    )


def cut_point(part: Part, accepted: int, bookends: bool) -> tuple[str, str | None]:
    """Where a stream cut after ``accepted`` bytes falls inside ``part``: (what to say, the resume range or
    None). The block in flight is the one holding the first byte the printer did not take."""
    at = accepted - part.offset  # the first byte not accepted, within the part
    pos, k = 0, 0
    for setup, qs in parse_blocks(part.data):
        k += 1
        pos += len(serialise_block(setup, qs))
        if at < pos:
            break
    rows = part.rows
    r = part.row
    if bookends and k == 1:
        return f"cut inside the START mark of {r.file} — none of its labels were accepted", r.labels
    if bookends and k == rows:
        return (
            f"cut inside the END mark of {r.file} — all its labels were accepted, only the END mark is missing",
            None,
        )
    row, n = (k - 1, rows - 2) if bookends else (k, rows)
    # the labels of that web row within the selection: resume from its first label to the selection's end
    picked = parse_selection(r.labels, r.total) if r.total else []
    blocks = sorted({(lab - 1) // part.per for lab in picked})
    resume = None
    if 0 < row <= len(blocks):
        first = min(lab for lab in picked if (lab - 1) // part.per == blocks[row - 1])
        resume = f"{first}-{picked[-1]}" if first != picked[-1] else f"{first}"
    return f"cut inside {r.file} at web row {row} of {n}", resume


def run(
    root: Path,
    reg: Registry,
    rows: list[PlanRow],
    log: Path | None,
    dry_run: bool = False,
    to: str | None = None,
    bookends: bool = False,
    proof: str | Path | None = None,
) -> list[str]:
    """Send a clean plan as ONE stream per printer (:func:`compose_streams`) — nothing while any row is refused.
    Notes: one per stream, then one per file with its verdict. EVERY file is logged (:func:`append_log`),
    success or failure, with its place in the stream. ``to`` overrides the transport for every stream (a
    tcp://, lp: or file path — a test spool); the printer's media and offset still apply. ``bookends`` puts a
    START block before and an END block after every file's labels. ``proof`` writes the composed stream(s) as
    a proof document (one file per printer when there are several). A cut stream: the files wholly before the
    cut are ok, the one in flight is logged with the web row the cut fell in and the resume range, the files
    after it are logged as not sent; nothing is ever resent here."""
    if any(r.refused for r in rows):
        return ["REFUSED rows in the plan — nothing sent (fix the cart and run again)"]
    streams = compose_streams(root, reg, rows, bookends)
    notes = [describe_stream(st, bookends) for st in streams]
    if proof:
        from .proof import proof_document

        out = Path(proof)
        for st in streams:
            dest = out if len(streams) == 1 else out.with_name(f"{out.stem}-{st.printer}{out.suffix}")
            n = proof_document(st.media, st.body, dest, caption=f"stream {st.id} → {st.printer}")
            notes.append(f"proof: {dest} ({n} web rows, {st.media.dpi} dpi)")
    if dry_run:
        notes.append("dry run — " + ("+2 bookends per file, " if bookends else "") + "nothing sent")
        return notes
    for st in streams:
        pr = reg.printers[st.printer]
        target = to or pr.transport
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            note, accepted = deliver(st.data, target)
            failure = None
        except Exception as e:  # SendError (bytes known), a USB or lp failure (bytes unknown = 0)
            accepted = int(getattr(e, "bytes_sent", 0) or 0)
            failure = f"{e.__class__.__name__}: {e}" if not isinstance(e, RuntimeError) else str(e)
            note = f"FAILED after {accepted} bytes of {st.length}: {failure}"
        heat = ""
        if pr.darkness is not None or pr.speed is not None:
            dk = pr.darkness if pr.darkness is not None else "file"
            sp = pr.speed if pr.speed is not None else "file"
            heat = f"; darkness {dk}, speed {sp}"
            note += f" ({heat[2:]})"
        notes.append(f"stream {st.id} → {st.printer}: {note}")
        for p in st.parts:
            r = p.row
            entry: dict[str, Any] = {
                "ts": ts,
                "file": r.file,
                "labels": r.labels,
                "count": r.count,
                "printer": pr.name,
                "transport": target,
                "note": "",
                "bookends": bookends,
                "ok": False,
                "bytes_sent": max(0, min(accepted - p.offset, p.length)),
                "stream": {"id": st.id, "offset": p.offset, "length": p.length},
            }
            r.stream = entry["stream"]
            if failure is None or p.end <= accepted:
                r.sent, r.error = True, None
                entry.update(ok=True, note=f"sent in stream {st.id} (bytes {p.offset}–{p.end}{heat})")
                line = f"{r.file} labels {r.labels}{' +2 bookends' if bookends else ''} → {entry['note']}"
            elif p.offset >= accepted:
                r.sent, r.error = False, f"not sent: stream {st.id} was cut before it"
                entry.update(error=r.error, note="not sent")
                line = f"{r.file} labels {r.labels} → NOT SENT (the stream was cut before it)"
            else:
                what, resume = cut_point(p, accepted, bookends)
                r.sent, r.error = False, what
                entry.update(error=what, note=f"CUT — labels ARE on the paper up to the cut ({failure})")
                line = f"{r.file} labels {r.labels} → CUT: {what}"
                if resume:
                    line += f"; resume with --labels {resume} — your call from what the roll shows; nothing is resent here"
            append_log(log, entry)
            notes.append(line)
        if failure is not None:
            break  # the streams for other printers wait for the operator
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
