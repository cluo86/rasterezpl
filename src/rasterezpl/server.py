"""``rasterezpl serve`` — a local web front end for printing: the browser is the GUI, this process holds the
printers, so the same page works from any machine and any OS that can run Python and reach the printer.

    rasterezpl serve --root DIR [--host 127.0.0.1] [--port 8123] [--registry printers.yaml] [--log FILE]

Serves the job files under ``--root`` (any ``.ezpl``, recursively) plus whatever static pages sit beside them,
and a JSON API the built-in page at ``/rasterezpl/`` uses — a page of your own served from the same root can use
it too (same origin; ``/api/`` also answers CORS preflights for a page served elsewhere):

    GET  /api/printers                     the registry's printers (name, transport, media, offset, note)
    GET  /api/printers/<name>/status       ~S,CHECK that printer (00 = ready)
    GET  /api/files                        every .ezpl under the root: blocks, labels, the media it matches
    GET  /api/proof?file=F&label=N         PNG proof of the web row holding label N (the exact dots, mm scale)
    POST /api/plan   {"jobs":[{"file":F,"labels":"1-4,7","printer":P?}, …]}
                                           the plan: per file the labels, count, printer (chosen by the file's
                                           media when omitted) and any refusal — nothing is sent
    POST /api/print  {"jobs":[…], "dry_run":false}
                                           the plan, then one job per file through the printer's record; refused
                                           while ANY row is refused (fix the cart, not half of it); every send is
                                           appended to the log (JSON lines)
    GET  /api/log?n=50                     the last n log entries

Guards are the CLI's: a file prints only on a printer holding the media it was written for, and only if every
block lies inside that media's print areas. Paths are confined to the root. The server binds 127.0.0.1 unless
told otherwise; a printer is a physical output, so put the page on the machine the printers hang off and let
people walk to it, or bind a LAN address knowingly.
"""

from __future__ import annotations

import io
import json
import re
import threading
import time
from dataclasses import asdict, dataclass
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .registry import Registry, load
from .stream import (
    blocks_outside_areas,
    count_labels,
    decode_block,
    matching_media,
    parse_blocks,
    parse_selection,
    select_labels,
)
from .transport import send, status

SPEC_RE = re.compile(r"^\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*$")
LOG_NAME = ".rasterezpl-printlog.jsonl"


@dataclass
class PlanRow:
    file: str  # relative to the root, as given
    labels: str  # the print-dialog spec
    count: int  # labels selected
    total: int  # labels in the file
    printer: str | None  # registry printer chosen or given
    media: str | None  # media the file matches (the printer's, when chosen)
    refused: str | None  # why it cannot go, else None


def _confine(root: Path, rel: str) -> Path:
    """The file for a request path, refusing anything outside the root or not a .ezpl."""
    p = (root / rel.lstrip("/")).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"{rel!r} is outside the served root")
    if p.suffix.lower() != ".ezpl":
        raise ValueError(f"{rel!r} is not a .ezpl job file")
    return p


def list_files(root: Path, reg: Registry) -> list[dict]:
    """Every .ezpl under the root with what the API needs to show it."""
    out = []
    for p in sorted(root.rglob("*.ezpl")):
        rel = p.relative_to(root).as_posix()
        data = p.read_bytes()
        row: dict = {"file": rel, "bytes": len(data), "blocks": 0, "labels": 0, "media": [], "printers": []}
        try:
            names = matching_media(data, reg.media)
        except ValueError as e:
            row["error"] = str(e)
            out.append(row)
            continue
        per = max((reg.media[n].labels_per_block for n in names), default=1)
        row["blocks"] = count_labels(data)
        row["labels"] = row["blocks"] * per
        row["media"] = names
        row["printers"] = [pr.name for pr in reg.printers.values() if pr.media_name in names]
        out.append(row)
    return out


def plan(root: Path, reg: Registry, jobs: list[dict]) -> list[PlanRow]:
    """One row per job: the file's labels, count, the printer (given, or the one registry printer holding the
    file's media) and the refusal, if any. Pure: nothing is sent."""
    rows: list[PlanRow] = []
    for j in jobs:
        file = str(j.get("file", ""))
        spec = str(j.get("labels", "") or "").replace(" ", "")
        given = j.get("printer") or None
        row = PlanRow(file, spec, 0, 0, given, None, None)
        rows.append(row)
        try:
            p = _confine(root, file)
        except ValueError as e:
            row.refused = str(e)
            continue
        if not p.exists():
            row.refused = "missing — regenerate its family"
            continue
        data = p.read_bytes()
        try:
            names = matching_media(data, reg.media)
        except ValueError as e:
            row.refused = str(e)
            continue
        if given:
            pr = reg.printers.get(given)
            if pr is None:
                row.refused = f"unknown printer {given!r}"
                continue
            if pr.media_name not in names:
                row.refused = (
                    f"{file} was written for {names or 'no known media'}, {pr.name} holds {pr.media_name}"
                )
                continue
        else:
            cands = [pr for pr in reg.printers.values() if pr.media_name in names]
            if not cands:
                row.refused = f"no printer on record holds {names or 'the media this file was written for'}"
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
        if picked[-1] > row.total:
            row.refused = f"label {picked[-1]} past the file's end ({row.total} labels)"
            continue
        bad = blocks_outside_areas(data, m)
        if bad:
            bn, bx, by = bad[0]
            row.refused = (
                f"{len(bad)} block(s) outside {pr.media_name}'s print areas (first: block {bn} at x {bx}, y {by})"
                " — written for another frame or geometry of this stock; regenerate it"
            )
    return rows


def run(root: Path, reg: Registry, rows: list[PlanRow], log: Path | None, dry_run: bool = False) -> list[str]:
    """Send every row of a clean plan, one job per file, through the printer's record; nothing while any row is
    refused. Returns one note per row; each real send is appended to the log as a JSON line."""
    if dry_run:
        return ["dry run — nothing sent"]
    if any(r.refused for r in rows):
        return ["REFUSED rows in the plan — nothing sent (fix the cart and run again)"]
    notes = []
    for r in rows:
        pr = reg.printers[r.printer or ""]
        data = select_labels(_confine(root, r.file).read_bytes(), r.labels, pr.media)
        note = send(data, pr.transport, pr.media, pr.offset_in)
        notes.append(f"{r.file} labels {r.labels} → {note}")
        if log is not None:
            entry = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "file": r.file,
                "labels": r.labels,
                "count": r.count,
                "printer": pr.name,
                "transport": pr.transport,
                "note": note,
            }
            with log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return notes


def proof_png(root: Path, reg: Registry, file: str, label: int) -> bytes:
    """The proof of the web row holding label `label` (1-based) as PNG bytes."""
    from .proof import proof_image

    p = _confine(root, file)
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
    if log is None or not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [json.loads(ln) for ln in lines[-n:] if ln.strip()]


UI_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>rasterezpl</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
body{font-family:system-ui,sans-serif;font-size:14px;margin:14px;color:#111;background:#fafafa}
h1{font-size:18px;margin:0 0 4px}h2{font-size:15px;margin:18px 0 6px}
table{border-collapse:collapse;background:#fff}td,th{border:1px solid #ccc;padding:3px 8px;text-align:left;vertical-align:top}th{background:#eee}
code{font-family:ui-monospace,Menlo,monospace;font-size:12px}input[type=text]{font-family:ui-monospace,Menlo,monospace}
.muted{color:#666;font-size:12px}.ok{color:#1a7f37}.bad{color:#b3261e;font-weight:600}
#cart{position:sticky;top:0;z-index:2;background:#fffbe6;border:1px solid #e0c14a;padding:6px 10px;margin:8px 0}
button.big{font-size:14px;padding:6px 14px}button.rm{font-size:11px;padding:0 5px}
img.proof{max-width:520px;border:1px solid #999}#note{white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace;font-size:12px}
</style></head><body>
<h1>rasterezpl — print from the browser</h1>
<div class=muted>Job files under the served root, the printers on record, a cart, one print. The plan is shown before anything is sent; a refused row stops the whole cart.</div>
<div id=cart></div>
<h2>Printers</h2><table><thead><tr><th>name</th><th>transport</th><th>media</th><th>offset (in)</th><th>note</th><th>status</th></tr></thead><tbody id=printers></tbody></table>
<h2>Files</h2><div class=muted>filter <input type=text id=filter size=30 placeholder="part of a path"> · labels as in a print dialog: <code>1-4,7</code>, empty = all</div>
<table><thead><tr><th>file</th><th>labels</th><th>media / printers</th><th>select</th><th>proof</th></tr></thead><tbody id=files></tbody></table>
<h2>Print log</h2><div id=log class=muted></div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let FILES=[],PRINTERS=[],CART=[];
try{CART=JSON.parse(localStorage.getItem("rasterezpl-cart")||"[]")}catch(e){CART=[]}
function saveCart(){try{localStorage.setItem("rasterezpl-cart",JSON.stringify(CART))}catch(e){}}
async function api(path,body){const r=await fetch(path,body?{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify(body)}:undefined);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
async function loadPrinters(){PRINTERS=await api("/api/printers");$("printers").innerHTML=PRINTERS.map(p=>`<tr><td><b>${esc(p.name)}</b></td><td><code>${esc(p.transport)}</code></td><td>${esc(p.media_name)}</td><td>${p.offset_in.map(v=>v.toFixed(3)).join(", ")}</td><td class=muted>${esc(p.note)}</td><td><button onclick="pstatus('${esc(p.name)}',this)">check</button> <span id="st-${esc(p.name)}"></span></td></tr>`).join("")}
async function pstatus(name,btn){btn.disabled=true;try{const j=await api(`/api/printers/${encodeURIComponent(name)}/status`);$("st-"+name).textContent=j.status||"no reply";$("st-"+name).className=(j.status||"").startsWith("00")?"ok":"bad"}catch(e){$("st-"+name).textContent=e.message;$("st-"+name).className="bad"}btn.disabled=false}
async function loadFiles(){FILES=await api("/api/files");renderFiles()}
function renderFiles(){const q=$("filter").value.trim().toLowerCase();$("files").innerHTML=FILES.filter(f=>!q||f.file.toLowerCase().includes(q)).map((f,i)=>{const idx=FILES.indexOf(f);const pr=f.printers.length===1?f.printers[0]:"";return `<tr><td><code>${esc(f.file)}</code>${f.error?`<div class=bad>${esc(f.error)}</div>`:""}</td><td>${f.labels} <span class=muted>(${f.blocks} rows)</span></td><td>${esc(f.media.join(", "))}<div class=muted>${f.printers.length?f.printers.join(", "):"no printer holds this media"}</div></td><td><input type=text id="sel-${idx}" size=10 placeholder="all"> <select id="pr-${idx}">${f.printers.map(p=>`<option ${p===pr?"selected":""}>${esc(p)}</option>`).join("")}</select> <button onclick="addToCart(${idx})">add to cart</button></td><td><a href="/api/proof?file=${encodeURIComponent(f.file)}&label=1" target=_blank>proof</a></td></tr>`}).join("")}
function addToCart(i){const f=FILES[i];const sel=$("sel-"+i).value.trim();const pr=$("pr-"+i).value;CART.push({file:f.file,labels:sel,printer:pr||undefined});saveCart();renderCart()}
function rmCart(i){CART.splice(i,1);saveCart();renderCart()}
function clearCart(){CART=[];saveCart();renderCart()}
let PLAN=null;
function renderCart(){PLAN=null;if(!CART.length){$("cart").innerHTML="<b>cart</b> <span class=muted>empty — add files or label ranges below</span>";return}
 $("cart").innerHTML=`<b>cart</b> ${CART.length} job(s) <button class=rm onclick="clearCart()">clear</button><table>${CART.map((c,i)=>`<tr><td><code>${esc(c.file)}</code></td><td>labels ${esc(c.labels||"all")}</td><td>${esc(c.printer||"auto")}</td><td><button class=rm onclick="rmCart(${i})">remove</button></td></tr>`).join("")}</table>
 <button class=big onclick="doPlan()">plan (nothing sent)</button> <button class=big id=printbtn disabled onclick="doPrint()">print</button><div id=plan></div><div id=note></div>`}
function planTable(rows){return `<table><tr><th>file</th><th>labels</th><th>count</th><th>printer</th><th>state</th></tr>${rows.map(r=>`<tr><td><code>${esc(r.file)}</code></td><td>${esc(r.labels)}</td><td>${r.count}/${r.total}</td><td>${esc(r.printer||"-")}</td><td class="${r.refused?"bad":"ok"}">${esc(r.refused||"ok")}</td></tr>`).join("")}</table>`}
async function doPlan(){try{const j=await api("/api/plan",{jobs:CART});PLAN=j.plan;const ok=!PLAN.some(r=>r.refused);const n=PLAN.reduce((a,r)=>a+r.count,0);$("plan").innerHTML=planTable(PLAN)+`<div class="${ok?"ok":"bad"}">${ok?`${n} labels ready — press print`:"fix the refused rows"}</div>`;$("printbtn").disabled=!ok;$("printbtn").textContent=ok?`print ${n} labels`:"print"}catch(e){$("plan").innerHTML=`<div class=bad>${esc(e.message)}</div>`}}
async function doPrint(){if(!PLAN)return;$("printbtn").disabled=true;$("note").textContent="sending…";try{const j=await api("/api/print",{jobs:CART});$("note").textContent=j.notes.join("\n");if(j.sent){CART=[];saveCart();loadLog();setTimeout(renderCart,4000)}}catch(e){$("note").textContent=e.message;$("note").className="bad"}}
async function loadLog(){const j=await api("/api/log?n=20");$("log").innerHTML=j.length?`<table>${j.slice().reverse().map(e=>`<tr><td>${esc(e.ts)}</td><td><code>${esc(e.file)}</code></td><td>${esc(e.labels)} (${e.count})</td><td>${esc(e.printer)}</td><td>${esc(e.note)}</td></tr>`).join("")}</table>`:"nothing printed yet"}
$("filter").oninput=renderFiles;
loadPrinters();loadFiles();renderCart();loadLog();
</script></body></html>
"""


class Handler(SimpleHTTPRequestHandler):
    """Static files from the root + the JSON API + the built-in page."""

    root: Path
    reg: Registry
    log_path: Path | None
    lock: threading.Lock

    def __init__(self, *a, root: Path, reg: Registry, log_path: Path | None, lock: threading.Lock, **kw):
        self.root, self.reg, self.log_path, self.lock = root, reg, log_path, lock
        super().__init__(*a, directory=str(root), **kw)

    # -- plumbing ---------------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # quieter than the default (one line per request is enough)
        if "/api/print" in (args[0] if args else "") or "error" in fmt.lower():
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        if self.path.startswith("/api/"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def _json(self, obj, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, body: bytes, ctype: str, code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            doc = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ValueError(f"bad JSON body: {e}") from e
        if not isinstance(doc, dict):
            raise ValueError("the body must be a JSON object")
        return doc

    # -- routes -----------------------------------------------------------------------------------------------
    def do_OPTIONS(self):  # CORS preflight for a page served from elsewhere
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        u = urlsplit(self.path)
        try:
            if u.path in ("/rasterezpl", "/rasterezpl/"):
                return self._bytes(UI_HTML.encode("utf-8"), "text/html; charset=utf-8")
            if u.path == "/api/printers":
                return self._json(
                    [
                        {
                            "name": pr.name,
                            "transport": pr.transport,
                            "media_name": pr.media_name,
                            "offset_in": list(pr.offset_in),
                            "note": pr.note,
                        }
                        for pr in self.reg.printers.values()
                    ]
                )
            m = re.fullmatch(r"/api/printers/([^/]+)/status", u.path)
            if m:
                name = m.group(1)
                pr = self.reg.printers.get(name)
                if pr is None:
                    return self._json({"error": f"unknown printer {name!r}"}, 404)
                try:
                    with self.lock:
                        st = status(pr.transport)
                except Exception as e:  # a printer off the bus is an answer, not a crash
                    return self._json({"printer": name, "status": "", "error": str(e)})
                return self._json({"printer": name, "status": st})
            if u.path == "/api/files":
                return self._json(list_files(self.root, self.reg))
            if u.path == "/api/proof":
                q = parse_qs(u.query)
                file = (q.get("file") or [""])[0]
                label = int((q.get("label") or ["1"])[0])
                return self._bytes(proof_png(self.root, self.reg, file, label), "image/png")
            if u.path == "/api/log":
                n = int((parse_qs(u.query).get("n") or ["50"])[0])
                return self._json(read_log(self.log_path, n))
            if u.path.startswith("/api/"):
                return self._json({"error": f"no such endpoint {u.path}"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        return super().do_GET()  # static: the job files, proofs and any page beside them

    def do_POST(self):
        u = urlsplit(self.path)
        try:
            doc = self._body()
            jobs = doc.get("jobs")
            if not isinstance(jobs, list) or not jobs:
                return self._json({"error": "jobs: a non-empty list of {file, labels, printer?}"}, 400)
            if u.path == "/api/plan":
                rows = plan(self.root, self.reg, jobs)
                return self._json({"plan": [asdict(r) for r in rows]})
            if u.path == "/api/print":
                rows = plan(self.root, self.reg, jobs)
                dry = bool(doc.get("dry_run"))
                refused = any(r.refused for r in rows)
                with self.lock:  # one job at a time on the bus
                    notes = run(self.root, self.reg, rows, self.log_path, dry_run=dry)
                sent = not dry and not refused
                return self._json(
                    {"plan": [asdict(r) for r in rows], "notes": notes, "sent": sent},
                    200 if sent or dry else 409,
                )
            return self._json({"error": f"no such endpoint {u.path}"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # a failed send is reported, never a stack trace to the page
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def make_server(
    root: Path, host: str = "127.0.0.1", port: int = 8123, registry: str | None = None, log: str | None = None
) -> ThreadingHTTPServer:
    root = root.resolve()
    reg = load(registry)
    log_path = Path(log).expanduser() if log else root / LOG_NAME
    handler = partial(Handler, root=root, reg=reg, log_path=log_path, lock=threading.Lock())
    return ThreadingHTTPServer((host, port), handler)


def serve(
    root: Path, host: str = "127.0.0.1", port: int = 8123, registry: str | None = None, log: str | None = None
) -> int:
    srv = make_server(root, host, port, registry, log)
    h, p = str(srv.server_address[0]), int(srv.server_address[1])
    print(f"rasterezpl serve: root {root.resolve()}  →  http://{h}:{p}/rasterezpl/   (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("bound to a non-loopback address: anyone who can reach this port can print on these printers")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0
