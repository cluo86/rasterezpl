"""``rasterezpl serve`` — the HTTP layer of the browser front end: the built-in page, the JSON API, and the job
files as static files. The behaviour lives in :mod:`rasterezpl.jobs` (files, plan, send, log) and
:mod:`rasterezpl.compose` (labels from text, rows or an Easy-Mark project); this module only routes.

    rasterezpl serve --root DIR [--host 127.0.0.1] [--port 8123] [--registry printers.yaml] [--log FILE] [--open]

Endpoints are listed by ``GET /api/`` and documented in ``docs/API.md``. Errors come back as JSON
``{"error": "…"}``: 400 for a bad request (a ValueError in the layer below), 404 for an unknown endpoint or
printer, 409 for a print refused by its plan, 500 for a failed send (the transport's own message).

The server binds 127.0.0.1 unless told otherwise: a printer is a physical output. ``/api/`` answers CORS preflights
so a page served elsewhere may drive it when the browser allows it, but the intended use is a page served from
the same root, same origin.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import webbrowser
from dataclasses import asdict
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .compose import Spec, compose, fonts_available, import_pemx, preview_png, read_spec
from .jobs import LOG_NAME, list_files, plan, proof_png, read_log, run
from .registry import Registry, load

ENDPOINTS: list[tuple[str, str, str]] = [
    ("GET", "/api/", "this list"),
    ("GET", "/api/version", "the package version"),
    ("GET", "/api/printers", "the registry's printers: name, transport, media_name, offset_in, note"),
    ("GET", "/api/printers/<name>/status", "~S,CHECK that printer (00 = ready)"),
    ("GET", "/api/media", "the registry's media: name, dpi, size, print areas, printers holding it"),
    (
        "GET",
        "/api/fonts",
        "faces this machine can print: bundled (OFL) + known candidates that resolve + <root>/fonts",
    ),
    ("GET", "/api/layouts", "the label layouts (a decoration around the text) with a sample text each"),
    ("GET", "/api/files", "every .ezpl under the root: blocks, labels, matching media, printers"),
    ("GET", "/api/proof?file=F&label=N", "PNG proof of the web row holding label N"),
    ("GET", "/api/spec?file=composed/x.json", "a composed job's spec (to reload the form)"),
    ("GET", "/api/log?n=50", "the last n print-log entries"),
    ("POST", "/api/plan", '{"jobs":[{"file","labels"?,"printer"?}]} → the plan, nothing sent'),
    ("POST", "/api/print", '{"jobs":[…],"dry_run"?} → plan + one job per file; 409 while any row is refused'),
    ("POST", "/api/compose", 'a Spec (+ "preview": true for the PNG of row 1) → composed/<name>-<hash>.ezpl'),
    ("POST", "/api/import", '{"pemx": <base64>} → an Easy-Mark project\'s texts, point size, part'),
]


def page_html() -> str:
    """The built-in page, a package asset."""
    return resources.files("rasterezpl").joinpath("web/index.html").read_text(encoding="utf-8")


class Handler(SimpleHTTPRequestHandler):
    """Static files from the root + the JSON API + the built-in page at /rasterezpl/."""

    root: Path
    reg: Registry
    log_path: Path | None
    lock: threading.Lock

    def __init__(self, *a, root: Path, reg: Registry, log_path: Path | None, lock: threading.Lock, **kw):
        self.root, self.reg, self.log_path, self.lock = root, reg, log_path, lock
        super().__init__(*a, directory=str(root), **kw)

    # -- plumbing ---------------------------------------------------------------------------------------------
    def log_message(self, fmt, *args):  # one line for a print or an error; static and polling stay quiet
        msg = str(args[0]) if args else ""
        code = str(args[1]) if len(args) > 1 else ""
        if "/api/print" in msg or "/api/compose" in msg or code.startswith(("4", "5")):
            super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        if self.path.startswith("/api/"):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
        super().end_headers()

    def _json(self, obj, code: int = 200) -> None:
        self._bytes(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code
        )

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

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

    # -- routes -----------------------------------------------------------------------------------------------
    def do_OPTIONS(self):  # CORS preflight for a page served from elsewhere
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self):
        path = urlsplit(self.path).path
        try:
            if path in ("/rasterezpl", "/rasterezpl/"):
                return self._bytes(page_html().encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/api/"):
                return self._api_get(path)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # never a stack trace to the page
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)
        return super().do_GET()  # static: the job files, proofs and any page beside them

    def _api_get(self, path: str) -> None:
        if path == "/api/":
            return self._json(
                {
                    "version": __version__,
                    "endpoints": [{"method": m, "path": p, "what": w} for m, p, w in ENDPOINTS],
                }
            )
        if path == "/api/version":
            return self._json({"version": __version__})
        if path == "/api/printers":
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
        m = re.fullmatch(r"/api/printers/([^/]+)/status", path)
        if m:
            return self._printer_status(m.group(1))
        if path == "/api/media":
            return self._json(
                [
                    {
                        "name": n,
                        "dpi": md.dpi,
                        "width_mm": md.width_mm,
                        "length_mm": md.length_mm,
                        "areas": len(md.areas_in),
                        "printers": [pr.name for pr in self.reg.printers.values() if pr.media_name == n],
                    }
                    for n, md in self.reg.media.items()
                ]
            )
        if path == "/api/fonts":
            return self._json(fonts_available(self.root))
        if path == "/api/layouts":
            from .layouts import LAYOUTS

            return self._json([{"name": lay.name, "sample": lay.sample} for lay in LAYOUTS.values()])
        if path == "/api/files":
            return self._json(list_files(self.root, self.reg))
        if path == "/api/proof":
            q = self._query()
            png = proof_png(self.root, self.reg, q.get("file", ""), int(q.get("label", "1")))
            return self._bytes(png, "image/png")
        if path == "/api/spec":
            return self._json(read_spec(self.root, self._query().get("file", "")))
        if path == "/api/log":
            return self._json(read_log(self.log_path, int(self._query().get("n", "50"))))
        return self._json({"error": f"no such endpoint {path}"}, 404)

    def _printer_status(self, name: str) -> None:
        from .transport import status

        pr = self.reg.printers.get(name)
        if pr is None:
            return self._json({"error": f"unknown printer {name!r}"}, 404)
        try:
            with self.lock:
                st = status(pr.transport)
        except Exception as e:  # a printer off the bus is an answer, not a crash
            return self._json({"printer": name, "status": "", "error": str(e)})
        return self._json({"printer": name, "status": st})

    def do_POST(self):
        path = urlsplit(self.path).path
        try:
            doc = self._body()
            if path == "/api/compose":
                spec = Spec.from_dict(doc)
                if doc.get("preview"):
                    png = preview_png(self.reg, spec, self.root, int(doc.get("block", 1) or 1))
                    return self._bytes(png, "image/png")
                return self._json(compose(self.root, self.reg, spec))
            if path == "/api/import":
                return self._json(import_pemx(base64.b64decode(str(doc.get("pemx", "")))))
            if path in ("/api/plan", "/api/print"):
                jobs = doc.get("jobs")
                if not isinstance(jobs, list) or not jobs:
                    return self._json({"error": "jobs: a non-empty list of {file, labels, printer?}"}, 400)
                rows = plan(self.root, self.reg, jobs)
                if path == "/api/plan":
                    return self._json({"plan": [asdict(r) for r in rows]})
                dry = bool(doc.get("dry_run"))
                refused = any(r.refused for r in rows)
                with self.lock:  # one job at a time on the bus
                    notes = run(self.root, self.reg, rows, self.log_path, dry_run=dry)
                sent = not dry and not refused
                return self._json(
                    {"plan": [asdict(r) for r in rows], "notes": notes, "sent": sent},
                    200 if sent or dry else 409,
                )
            return self._json({"error": f"no such endpoint {path}"}, 404)
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:  # a failed send is reported, never a stack trace to the page
            return self._json({"error": f"{type(e).__name__}: {e}"}, 500)


def make_server(
    root: Path, host: str = "127.0.0.1", port: int = 8123, registry: str | None = None, log: str | None = None
) -> ThreadingHTTPServer:
    """A configured server, not yet serving (tests call ``serve_forever`` in a thread)."""
    root = root.resolve()
    reg = load(registry)
    log_path = Path(log).expanduser() if log else root / LOG_NAME
    handler = partial(Handler, root=root, reg=reg, log_path=log_path, lock=threading.Lock())
    return ThreadingHTTPServer((host, port), handler)


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8123,
    registry: str | None = None,
    log: str | None = None,
    open_browser: bool = False,
) -> int:
    """Serve until Ctrl-C. ``open_browser`` opens the page in the default browser once the socket is bound."""
    srv = make_server(root, host, port, registry, log)
    h, p = str(srv.server_address[0]), int(srv.server_address[1])
    url = f"http://{h}:{p}/rasterezpl/"
    print(f"rasterezpl {__version__} serve: root {root.resolve()}  →  {url}   (Ctrl-C to stop)")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("bound to a non-loopback address: anyone who can reach this port can print on these printers")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        srv.server_close()
    return 0
