# rasterezpl HTTP API

Served by `rasterezpl serve --root DIR` (default `http://127.0.0.1:8123`). All responses are JSON unless noted.
`GET /api/` returns this list with the running version. Every error is `{"error": "…"}` with:

| code | meaning |
|---|---|
| 400 | a bad request — malformed JSON, an unknown media or face, a label selection outside the file, a path outside the root |
| 404 | no such endpoint or printer |
| 409 | a print refused by its plan — the plan is returned, nothing was sent |
| 500 | a failed send or an unexpected error — the message, never a stack trace |

Paths in requests are relative to the served root and confined to it; only `.ezpl` files are jobs.

## Concepts

- **Root** — the directory of job files (`.ezpl`, recursively). Anything else in it is served as a static file,
  so a page of your own can live beside the jobs and call this API on the same origin.
- **Registry** — printers and media from `printers.yaml` (see the README). A printer has a transport, a media
  and a registration offset, and optionally a `darkness` (^H 0–19) and `speed` (^S ips) applied to every job at
  send time — the file stays unchanged; a head that prints light gets more heat from its own record. A job file matches a media by its header and geometry; it prints only on a printer
  holding that media — or the same stock at the same dpi (a variant that only differs in where blocks may go,
  such as the whole label of a self-laminating stock instead of its white block).
- **Plan** — a cart of `{file, labels, printer?}` entries resolved to rows: labels selected, count, the printer
  (given, or the one printer holding the file's media), or a refusal. Planning never sends.
- **Print** — a clean plan sent as one job per file through the printer's record; refused entirely while any row
  is refused. Every send is appended to the log.
- **Spec** — the composer's input: media, font, size, alignment, copies, and the labels as lines of text.

## GET endpoints

### `GET /api/version`
`{"version": "0.3.0"}`

### `GET /api/printers`
```json
[{"name": "tdp43me", "transport": "usb:0123ABCD", "media_name": "panduit-s150x225vaty-2up",
  "offset_in": [0.013, 0.03], "note": "ruler-read 2026-09-26"}]
```

### `GET /api/printers/<name>/status`
`~S,CHECK` on that printer. `{"printer": "tdp43me", "status": "00"}` — `00` is ready; an empty status with an
`error` means the printer did not answer (off the bus, wrong transport).

### `GET /api/media`
```json
[{"name": "panduit-s150x225vaty-2up", "dpi": 300, "width_mm": 82.55, "length_mm": 57.15, "areas": 2,
  "printers": ["tdp43me"]}]
```

### `GET /api/fonts`
Faces this machine can print: the bundled OFL faces first (`"bundled": true` — Inter, Inter Bold, JetBrains
Mono, Bebas Neue; they render the same on every machine), then the known candidates that resolve, then any
`.ttf`/`.otf` under `<root>/fonts`.
`[{"name": "Inter", "path": "…/rasterezpl/fonts/Inter-Regular.ttf", "bundled": true}, {"name": "arial", "path": "…"}]`

### `GET /api/layouts`
The label layouts, each with a sample text: `[{"name": "banner", "sample": "PATCH PANEL A\nrack 12 · U31\n…"}, …]`.
Layouts: `plain`, `framed`, `banner`, `sidebar`, `corners`, `ticket`; with a picture `logo`, `logo-top`, `image`; with a QR `badge`, `qr-left`, `qr`; with a Code 128 barcode `barcode` (bars across the top, lines below), `barcode-below` (lines on top, bars under them), `barcode-only`; for a self-laminating label printed whole (a `*-full` media) `clear-qr`, `clear-image` — text on the white block, the code or picture on the clear laminate. Each entry also says `needs_image` / `needs_qr` and gives a `qr_sample`.

### `GET /api/files`
Every job under the root.
```json
[{"file": "panduit/labels-6203-U31.ezpl", "bytes": 743210, "blocks": 32, "labels": 64,
  "media": ["panduit-s150x225vaty-2up"], "printers": ["tdp43me"]}]
```
A file whose header matches no media carries `"error"` instead of media.

### `GET /api/proof?file=F&label=N`
`image/png`: the proof of the web row holding label `N` (1-based, default 1) — the exact dots on the physical
label with a mm scale.

### `GET /api/spec?file=composed/x.json`
A composed job's sidecar: the Spec it was made from, the font path, the expanded texts, when.

### `GET /api/composed?n=30`
The composed jobs under `<root>/composed`, newest first — the recipe list:
```json
[{"spec": "composed/dco-1a2b3c4d.json", "file": "composed/dco-1a2b3c4d.ezpl", "name": "dco", "created": "2026-10-15T14:02:11",
  "labels": 12, "media": "panduit-s150x225vaty-2up", "layout": "barcode-below", "font": "Inter", "pt": 5.4, "input": "rows"}]
```
`GET /api/spec?file=…` on the `spec` path gives the full recipe (every Spec field, the `rows` and `template`
included; the picture is not kept).

### `GET /api/log?n=50`
The last `n` print-log entries, oldest first:
`[{"ts": "2026-09-30T12:07:01", "file": "…", "labels": "1-4", "count": 4, "printer": "tdp43me",
  "transport": "usb:0123ABCD", "note": "sent in stream 3f2a9c1e (bytes 10–23797)",
  "bookends": false, "ok": true, "bytes_sent": 23787, "stream": {"id": "3f2a9c1e", "offset": 10, "length": 23787}}]`
`count` is the labels; `bookends` says whether a START and an END block went around them (0.13.0). Since 0.14.0
EVERY send is logged, success or failure — from `print`, `printcart` and this API — with `ok`, `bytes_sent` (the
bytes of THIS file the printer accepted) and, on a failure, `error` and a `note` saying what is on the paper.
Since 0.16.0 a cart's files share one stream per printer and each line carries `stream`: `id` (one per stream),
`offset` (the file's first byte in the stream as sent, registration prefix counted) and `length`; a cut stream
logs the files before the cut `ok: true`, the one in flight `ok: false` with `error: "cut inside F at web row
N of M"` and `note: "CUT — labels ARE on the paper up to the cut (…)"`, the files after `ok: false`, `note:
"not sent"`. A single `print` logs without `stream`. The keys of older lines are unchanged; lines written before
0.13.0 / 0.14.0 / 0.16.0 lack the newer keys.

## POST endpoints

### `POST /api/plan`
```json
{"jobs": [{"file": "a/b.ezpl", "labels": "1-4,7", "printer": "tdp43me"},
          {"file": "c/d.ezpl"}]}
```
`labels` is a print-dialog selection (`1`, `1-2`, `1,3,5-6`; 1-based); omitted = the whole file. `printer`
omitted = the one registry printer holding the file's media (two candidates → refused: choose one).
```json
{"plan": [{"file": "a/b.ezpl", "labels": "1-4,7", "count": 5, "total": 64, "printer": "tdp43me",
           "media": "panduit-s150x225vaty-2up", "refused": null, "rows": 3,
           "sent": null, "error": null, "stream": null}]}
```
`rows` = the web rows the selection sends. `sent`, `error` and `stream` are filled by print.

### `POST /api/print`
Same body as plan, plus `"dry_run": true` to stop after the plan and `"bookends": true` to put a START block
before and an END block after EVERY file's labels (off by default). The clean plan is sent as ONE STREAM PER
PRINTER: the registration prefix once, the darkness / speed retuned once in front, then every file for that
printer in cart order (START, blocks, END with bookends) over one connection — see the README, *Long jobs on an
Ethernet board*. There is no gap between files and nothing to wait for; `"settle"` is ignored.
```json
{"plan": [{"file": "a/b.ezpl", "…": "…", "sent": true, "error": null,
           "stream": {"id": "3f2a9c1e", "offset": 10, "length": 612345}}],
 "notes": ["stream 3f2a9c1e → tdp43me: 2 files, 70 blocks (4 bookends), 1,273,901 bytes — a/b.ezpl labels 1-4,7 (3 rows) · …",
           "stream 3f2a9c1e → tdp43me: sent 1273901 bytes (70 labels) to 192.168.8.136:9100",
           "a/b.ezpl labels 1-4,7 +2 bookends → sent in stream 3f2a9c1e (bytes 10–612355)", "…"],
 "sent": true, "bookends": true}
```
`sent` is false (and the status 409) while any row is refused: nothing goes to any printer until the cart is
clean. With `dry_run` the status is 200, the notes describe each stream (files, blocks, bookends, bytes) and end
with `dry run — nothing sent` (with bookends: `dry run — +2 bookends per file, nothing sent`).

Before each stream's connection the port is waited on (refused / timed out / closed at once = not ready, retried
with backoff for up to 900 s). A stream the printer cuts is reported file by file: the files wholly before the
cut have `sent: true`; the file in flight `sent: false` with `error` naming the web row the cut fell in (`cut
inside c/d.ezpl at web row 12 of 66`) and a note with the resume range; the files after it `sent: false`,
`error: "not sent: stream … was cut before it"`. The response is then 500 with `"error"` and the plan. Nothing
is ever resent by the server; the operator reads the roll and resumes with a `labels` range.

### `POST /api/compose`
A Spec, in one of three input forms:

| field | meaning |
|---|---|
| `media` | a media name from `/api/media` |
| `font` | a face name from `/api/fonts` (or a file under `<root>/fonts`) — no substitution: a missing face is a 400 |
| `pt` | point size |
| `text` | labels as text: one per block, blocks separated by a blank line; `{n}` = running number |
| `rows` + `template` | a sheet as text — a header line and rows, delimiter sniffed among tab / comma / semicolon, BOM and CRLF tolerated, blank rows skipped; the template's `{column}` fields are filled per row, `\n` = new line, `{n}` = row number; no template = every column on its own line. Both are kept in the sidecar |
| `header` | with `rows`: the first line names the columns (default true); `false` names them `col1`, `col2`, … |
| `labels` | the labels already as a list of strings (lines joined by `\n`) |
| `name` | the file stem (default `labels`) |
| `align` | `center` (default) or `left` |
| `fit` | shrink the font until the widest line fits (default false) |
| `stroke` | extra dots of ink around every glyph: 0 (default), 1 or 2 — bolder, for a head that drops thin strokes |
| `copies` | copies of each label (default 1) |
| `start` | the first `{n}` (default 1) |
| `rotate180` | turn the print on the label (default false) |
| `layout` | a decoration around the text: `plain` (default), `framed`, `banner`, `sidebar`, `corners`, `ticket`; with a picture: `logo`, `logo-top`, `image` (`/api/layouts`) |
| `image` | base64 (or a `data:` URL) of a PNG, JPEG or SVG for the picture layouts; an SVG needs `cairosvg` or `rsvg-convert` on the server |
| `image_mode` | `dither` (default, greys as a halftone) or `threshold` (only what is darker than mid-grey) |
| `qr` | what the QR layouts (`badge`, `qr-left`, `qr`) and the Code 128 layouts (`barcode`, `barcode-below`, `barcode-only`) encode — a template filled per label: `{n}`, or `{column}` with rows input, or a fixed text; one code per label |
| `qrs` | the per-label QR data as a list, instead of `qr` (parallel to `labels`) |
| `area` | a single print area on multi-up media (default: fill areas in order) |
| `preview` | `true` → the response is `image/png`, the proof of `block` (default 1), and nothing is saved |

Response: `{"file": "composed/name-1a2b3c4d.ezpl", "spec": "composed/name-1a2b3c4d.json", "labels": 6,
"blocks": 3, "media": "…", "font_path": "…"}`. The file name carries the first 8 hex digits of the job's SHA-256,
so the same content composes to the same file.

### `POST /api/rows`
`{"rows": "<a sheet as text>", "template": "{name}\nR{rack}", "header": true}` → what the page shows before
composing: `{"columns": ["name", "rack"], "rows": 12, "labels": 12, "sample": ["Alice\nR6222", …]}` (the first
three labels), or with a template field the header lacks, `columns`, `rows` and an `error` naming the field.

### `POST /api/import`
`{"pemx": "<base64 of an Easy-Mark Plus .pemx>"}` →
`{"texts": ["line 1\nline 2", …], "pt": 5.4, "part": "S150X225VATY", "labels": 512}`.
The texts are every series' data across the project's documents, in order; `pt` is the first series' font size;
`part` is the format's part name. Feed `texts` to `/api/compose` as `labels` after choosing the media.

## CORS

`/api/` answers `OPTIONS` and sends `Access-Control-Allow-Origin: *`, so a page served from another origin may
call it if the browser permits the request to a loopback address. The intended use is a page served from the
same root.
