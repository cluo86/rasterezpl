# Changelog

## 0.5.0 — 2026-09-30

- Bundled faces (SIL OFL, `fonts/LICENSES.md`): Inter, Inter Bold, JetBrains Mono, Bebas Neue — a label renders
  the same on every machine; `find_font` resolves them by name, `/api/fonts` lists them first.
- Layouts (`rasterezpl.layouts`): a decoration around the text — `framed`, `banner`, `sidebar`, `corners`,
  `ticket`, or `plain` — each with a sample text; the composer's `layout` field, `/api/layouts`, a layout select
  and a *sample* button on the page. The demo root ships `templates/layouts.ezpl`, every layout filled.

## 0.4.0 — 2026-09-30

- `rasterezpl demo [DIR]`: a playground with no printer — demo jobs (text labels on two media, a checkerboard,
  stripes, a dithered gradient, the calibration ruler) and a registry whose printers are file spools, then the
  page on it. The whole pipeline runs; the bytes land in `spool/`.
- `header_of` / `matching_media` accept a job with the registration prefix in front (`^R`, `~Q`), so a spooled
  file decodes like the job it came from.

## 0.3.1 — 2026-09-30

- `jobs.run(..., to=…)`: override the transport for every row (a test spool); the log records the target used.

## 0.3.0 — 2026-09-30

- `rasterezpl.jobs`: the print-job layer on its own — `list_files`, `plan` / `PlanRow`, `format_plan`, `run`,
  `proof_png`, `read_log`, `parse_cart`. The web front end and the CLI share it.
- `rasterezpl printcart FILE[:LABELS] … [--root DIR] [--dry-run]`: several files, one plan, one print, logged.
- `rasterezpl serve --open`: opens the page. The page is a package asset (`rasterezpl/web/index.html`).
- `GET /api/` lists the endpoints with the version; `GET /api/version`. `docs/API.md` documents the API.
- `rasterezpl text` renders through the composer (one code path for text labels).
- The server module routes only; errors are JSON with a consistent status model.

## 0.2.0 — 2026-09-30

- `rasterezpl serve`: the browser front end — job files under a root, the registry's printers with status, a
  cart, plan then print, every send logged to `<root>/.rasterezpl-printlog.jsonl`, JSON API for a page beside
  the jobs.
- `rasterezpl.compose`: labels of your own — typed text (`{n}` running number, copies), pasted rows with a
  `{column}` template, or an Easy-Mark `.pemx` import; preview is the exact print; the result is an ordinary
  job under `composed/` with a `.json` sidecar (the spec). No silent font substitution.

## 0.1.0 — 2026-09-26

- The driverless EZPL raster path: media model and presets, job stream (blocks, selection, decode), text
  rendering, USB / TCP / lp / file transports, registration offsets, the calibration ruler, the proof image,
  the printer registry, the CLI.
