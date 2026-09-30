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
  and a registration offset. A job file matches a media by its header and geometry; it prints only on a printer
  holding that media.
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
Layouts: `plain`, `framed`, `banner`, `sidebar`, `corners`, `ticket`, and with a picture `logo`, `logo-top`, `image`.

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

### `GET /api/log?n=50`
The last `n` print-log entries, oldest first:
`[{"ts": "2026-09-30T12:07:01", "file": "…", "labels": "1-4", "count": 4, "printer": "tdp43me",
  "transport": "usb:0123ABCD", "note": "sent 23787 bytes (2 labels) to Panduit TDP43ME over USB"}]`

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
           "media": "panduit-s150x225vaty-2up", "refused": null}]}
```

### `POST /api/print`
Same body as plan, plus `"dry_run": true` to stop after the plan.
```json
{"plan": [...], "notes": ["a/b.ezpl labels 1-4,7 → sent … over USB"], "sent": true}
```
`sent` is false (and the status 409) while any row is refused: nothing goes to any printer until the cart is
clean. With `dry_run` the status is 200 and `notes` is `["dry run — nothing sent"]`.

### `POST /api/compose`
A Spec, in one of three input forms:

| field | meaning |
|---|---|
| `media` | a media name from `/api/media` |
| `font` | a face name from `/api/fonts` (or a file under `<root>/fonts`) — no substitution: a missing face is a 400 |
| `pt` | point size |
| `text` | labels as text: one per block, blocks separated by a blank line; `{n}` = running number |
| `rows` + `template` | a header line and TSV/CSV rows; the template's `{column}` fields are filled per row, `\n` = new line; `{n}` = row number |
| `labels` | the labels already as a list of strings (lines joined by `\n`) |
| `name` | the file stem (default `labels`) |
| `align` | `center` (default) or `left` |
| `fit` | shrink the font until the widest line fits (default false) |
| `copies` | copies of each label (default 1) |
| `start` | the first `{n}` (default 1) |
| `rotate180` | turn the print on the label (default false) |
| `layout` | a decoration around the text: `plain` (default), `framed`, `banner`, `sidebar`, `corners`, `ticket`; with a picture: `logo`, `logo-top`, `image` (`/api/layouts`) |
| `image` | base64 (or a `data:` URL) of a PNG, JPEG or SVG for the picture layouts; an SVG needs `cairosvg` or `rsvg-convert` on the server |
| `image_mode` | `dither` (default, greys as a halftone) or `threshold` (only what is darker than mid-grey) |
| `area` | a single print area on multi-up media (default: fill areas in order) |
| `preview` | `true` → the response is `image/png`, the proof of `block` (default 1), and nothing is saved |

Response: `{"file": "composed/name-1a2b3c4d.ezpl", "spec": "composed/name-1a2b3c4d.json", "labels": 6,
"blocks": 3, "media": "…", "font_path": "…"}`. The file name carries the first 8 hex digits of the job's SHA-256,
so the same content composes to the same file.

### `POST /api/import`
`{"pemx": "<base64 of an Easy-Mark Plus .pemx>"}` →
`{"texts": ["line 1\nline 2", …], "pt": 5.4, "part": "S150X225VATY", "labels": 512}`.
The texts are every series' data across the project's documents, in order; `pt` is the first series' font size;
`part` is the format's part name. Feed `texts` to `/api/compose` as `labels` after choosing the media.

## CORS

`/api/` answers `OPTIONS` and sends `Access-Control-Allow-Origin: *`, so a page served from another origin may
call it if the browser permits the request to a loopback address. The intended use is a page served from the
same root.
