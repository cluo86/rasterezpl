# Changelog

## 0.22.0 — 2026-11-08

- A fake printer: `rasterezpl fakeprinter`, `rasterezpl.FakePrinter`, the `fake_printer` pytest fixture. The
  TDP43ME's NetPS board taught its lessons by losing labels (2026-10-16/17) and `send_tcp` / `printcart` encode
  them, but nothing pinned those lessons except hand-written listening sockets inside the tests. The fake is
  the board on a TCP port with its vices as knobs: a buffer (`buffer_bytes`) printed out at a rate
  (`drain_bps`) — the port stops reading while the buffer is full, so the sender's TCP window fills and the
  chunked send rides the stall exactly as on the board, and "sent" is visibly not "printed" (`busy`); a NEW
  connection while busy is reset before any byte (`reset_while_busy`) and the listening socket then closes for
  `wedge_s` so connecting is refused, as the board's print server restart does; a connection is cut after
  `cut_after_bytes`; `~S,CHECK` is answered with `status_reply`. `PROFILES["netps"]` carries the measured
  numbers (8 KB/s, 4 MB buffer, resets, 90 s wedge, cut at 7,229,016 bytes); `ideal` has none. Every byte is
  recorded per connection with a verdict (accepted / RESET / CUT / status), `--record DIR` writes `job-<n>.ezpl`,
  `.decode(media)` shows the pages the roll would carry.
- Tests against it, on every OS of the matrix: a job and its decode; the stall and `busy`; a reset before the
  first byte retried until the board is free and nothing printed twice; a wedge refused and waited out, and the
  ready deadline honoured with 0 bytes; a cut naming the bytes, with the fake's accepted count never above the
  sender's; `printcart` cut inside file two of three reporting a resume row that is never BEFORE the paper's
  last complete row (the sender counts bytes handed to the kernel, the board counts bytes read — the reported
  row can only be later, which is why the README calls it the operator's call); the CLI end to end in a
  subprocess, the recorded job byte for byte. The lesson of writing them: the kernel's loopback buffers hide a
  few hundred KB of a stall, so a stall test needs a job that dwarfs them (16 MB).
- The release workflow's smoke test now runs the Windows executable against its own fake printer: the job sent
  over `tcp://` is byte for byte the job written to the demo's file spool, registration prefix included.

## 0.21.0 — 2026-11-08

- The Windows executable for a laptop without Python — the real goal of the port: GoLabel and EasyMark live on
  site laptops that have neither Python nor a Mac beside them. `packaging/rasterezpl.spec` is a ONE-FOLDER
  PyInstaller build carrying the bundled faces, the web page and pywin32; `.github/workflows/release.yml` builds
  it on `windows-latest`, runs it through the demo, a dry run with its proof, the ruler, a packed cart with
  bookends to the file spool, `decode` and `queues`, and on a `v<version>` tag attaches
  `rasterezpl-<version>-windows-x64.zip` to the GitHub release with that version's CHANGELOG section as the
  notes (the tag must match the package version). Built and smoke-tested the same way on a real Windows 11
  machine with a Godex G500 on its spooler (39 MB folder, 5 MB exe); not yet printed from — the owner's gate.
- The one-rendering promise is now CHECKED, not claimed: `.github/scripts/fingerprint.py` composes the demo's
  bundled-face jobs (every layout, QR, Code 128, the clear-laminate sheet, the patterns) and a packed cart to
  the file spool, and hashes the `.ezpl` bytes. `ci.yml` does it on all nine OS × Python jobs and a `compare`
  job demands one answer; `release.yml` compares the executable's rendering on Windows with the source tree's
  on Linux byte for byte. On 2026-11-08 the six hashes agreed across Ubuntu, macOS, Windows CI runners, a
  developer Mac and a real Windows 11 machine — after the 0.20.0 engine pin; before it they would not have.
  Left out on purpose: the text jobs set in the OS's Arial / DejaVu Sans, the ruler, and anything with a
  timestamp. First lesson of the check itself: a fingerprint written in text mode on Windows is CRLF and
  `diff` disagrees with itself — it is written LF everywhere.
- The `usb:` message names the libusb install for the OS at hand (Homebrew on a Mac, apt on Linux, the queue
  on Windows) instead of `brew` everywhere.

## 0.20.0 — 2026-11-08

- The Windows port. Every advantage over the vendor tools (one rendering, the stock guard, bookends, packing,
  the log, one stream per board) needed a Mac or Linux beside the printer, while the site laptops that GoLabel
  and EasyMark live on run Windows. CI now runs the suite on Windows, macOS and Linux (3.11–3.13), and that
  matrix was the survey of the port — three findings, each fixed:
  - **The raster depended on the machine.** The first red run was a bundled-face test: Inter Bold at 100 dots/em
    set `IAD108.1 SC77 R6225/A` 1162 dots wide on the Windows and macOS runners and 1150.9 on Ubuntu and the
    developer's Mac. Pillow uses the raqm engine (HarfBuzz, kerning) wherever libfribidi happens to be
    installed — Homebrew, Ubuntu — and FreeType's basic engine elsewhere. "What you preview is what prints" was
    true on one machine; two machines did not agree. Every font load (`render_text`, the bookends, the ruler,
    the proof caption) now goes through `load_font`, pinned to the basic engine that is inside every Pillow
    wheel: one raster everywhere, kerning lost everywhere. A label composed before 0.20.0 on a machine with
    fribidi may differ from its recomposition by a dot or two of letter spacing — the pinned stream tests are
    unchanged (they compare paths of one run), the one test at the fit limit was two dots past it and is set
    smaller.
  - A Windows temp path pasted into a YAML double-quoted string is read as escapes (`"C:\Users…"` → `\U`
    "expected 8 hexadecimal numbers"): the test fixtures now spell paths POSIX-style inside YAML, as the demo
    registry always did. The files the tool writes are read back as UTF-8 explicitly (Windows' default codec is
    cp1252 and the log carries `—` and `→`). A connection reset in the fake-port tests is a `SO_LINGER` struct
    of two shorts on Windows, two ints elsewhere. A fixed sleep after a slow send was not enough on a slow
    runner; the reader is joined. `.gitattributes` pins LF.
  - `confine()` resolves the root as well as the file (an 8.3 short name or a symlinked temp dir resolved on one
    side only read as "outside the served root").
- `win:<queue name>`: a Windows print queue written through the spooler with the RAW datatype — the bytes pass
  through unchanged to the queue's port, so the vendor driver the laptop already has is the pipe and no WinUSB /
  libusb swap is needed. Chunked like the TCP send; a failure mid-document aborts the spooled job and raises a
  `SendError` naming the bytes the spooler took (what reached the printer depends on the queue's spooling
  setting: read the roll); an unknown queue is a refusal with nothing sent. `status` over `win:` reports the
  spooler's status word and the jobs waiting and never `00` (no `~S,CHECK` through a queue), so `print --status`
  refuses as over `lp:`. Extra `rasterezpl[windows]` (pywin32); the module is imported on demand, so the package
  imports everywhere. Tested against a recorded fake of the API on every OS; the real print from a Windows
  laptop is the gate still open.
- `rasterezpl queues`: the Windows queues (name, port, driver, status, jobs, default) for `win:`, or on macOS /
  Linux the CUPS queues `lpstat` knows for `lp:`.
- `usb:` on Windows says why it cannot (usbprint.sys owns the device; Zadig would swap a WinUSB driver in) and
  points at `win:` — instead of a `NoBackendError` traceback. Off Windows the missing-backend message names
  libusb.
- The registry is looked for at `%APPDATA%\rasterezpl\printers.yaml` first (wherever `APPDATA` is set), then
  `~/.config/rasterezpl/printers.yaml`; `rasterezpl printers` with no file says where it looked and where to put
  one. `--registry` and `$RASTEREZPL_PRINTERS` unchanged.
- Fonts: the OS font folders are enumerated (`system_fonts`: `C:\Windows\Fonts` and the per-user folder on
  Windows, the Library folders on macOS, the share folders on Linux), each face under the name it gives itself,
  read with Pillow — no font registry; `find_font` resolves those names after the bundled faces and the known
  candidates, and `/api/fonts` lists them marked `system`. The proof caption uses the bundled Inter instead of a
  Mac path for Arial.
- The console tolerates a codec that cannot encode `→` / `·` (prints `?` instead of dying when output is
  redirected under cp1252). `python -m rasterezpl` runs the CLI. `status` exits with the message, not a trace,
  when a transport's library is missing.
- CI: failing tests are annotated on the run (file, line, message) so a matrix failure is readable without the
  job log. README: a *Windows* section, the layout-engine lesson, the status.

## 0.19.0 — 2026-10-25

- The registration prefix is ALWAYS sent — `^R<n>` and `~Q<±n>` both, a zero as `^R0` / `~Q+0` — in front of
  every send, the ruler included (`deliver` with a media; a send without one is the bytes as given). The
  printer keeps `^R` / `~Q` between jobs (its self-test slip prints the stored values), so a job that stated
  nothing printed with the registration the LAST job left: a second stock on the same TDP43ME, recorded with
  "no offset", inherited the Panduit record's +24 / +9 dots, and the calibration ruler printed "without an
  offset" read 24 dots off (2026-10-25). A record with an offset sends the same bytes as before; a zero-offset
  send gains the 10-byte prefix.
- `Media.same_stock` also requires the same `rotate180` and the same `labels_in`: a self-laminating stock
  with the Panduit S150X225VATY's page but the die turned round on the web (the white block at the trailing
  end) is `rotate180: false` with the same areas — the block at the other end of the printer frame — and a
  job for one lands on the other's clear tail, upside down. The planner now keeps the two apart (a file for
  one is never held by a printer carrying the other); the block / whole-label variants of one stock stay
  interchangeable as before. README (the lessons), tests.

## 0.18.0 — 2026-10-17

- Bookends cover the WHOLE die-cut label when the media knows its outline (`labels_in`) and it is larger than
  the print area — the Panduit S150X225VATY stock: START / END stay on the black 0.75 in block (the word now
  fills it; the END keeps its CUT HERE band), and the details (file, labels of total, count · printer · time;
  the END legend) go on the clear tail in black, in the largest bold face at which they fit, a hazard band at
  the label's far end. The tail takes ink like the block and on a work tag it was wasted; the white-on-black
  details on the block alone were barely legible on the roll (2026-10-17). Lines of fields break between the
  fields first, a line that must break is split evenly. A media without `labels_in`, or whose label is its
  print area, keeps the 0.13.0 block-only mark byte for byte. The tail ink lies outside the media's print
  areas by design: a bookend block is never checked against them (the labels still are).

## 0.17.0 — 2026-10-17

- A partial selection is PACKED into full rows on a multi-across web. The cart selects at label granularity
  (`labels-mtp-B29-6210-6122.ezpl:2,4,6,8,10,14,16` — the T2-end copies of seven cables, every one the right
  area of its row on the 2-across Panduit S150X225VATY web); `select_labels` kept each label in its own block
  with the other area blank, so seven wanted labels spent 14 labels of stock. Now the selected labels, in label
  order, are laid into rows area by area — row 1 = areas 0, 1; row 2 = areas 0, 1; … — so no row carries a
  blank except possibly the last: the seven become 4 rows. Each label is the crop of its original block at its
  area's rect, re-encoded at the destination rect on the file's own setup bytes; a packed row that is exactly an
  original block passes through byte for byte. Only between print areas of the same size in dots; a media
  whose areas differ is never packed (the labels keep their rows, the summary says why). A selection that is
  whole rows anyway (`1-128`, the whole file, one full row) is byte-identical to 0.16.0 — the pin for every job
  printed so far; packing happens only when at least one row is partly selected.
- Default on. `--no-pack` on `print` and `printcart`, `"pack": false` on `POST /api/plan` / `/api/print`, and
  the page's *pack partial rows* checkbox (ticked by default) keep every label in its original row and area.
  Packing is per FILE inside a cart, never across files: a web row never mixes two files, the bookends stay per
  file. The plan row says `2,4,6,8,10,14,16 → 4 rows (packed)`, the stream description `7 labels → 4 rows,
  packed`, `print` `would send 4 blocks … — 7 labels → 4 rows (packed)`; the log line gains `rows` and
  `packed`, `labels` keeps the selection as given (the paper is traced by label numbers); `--proof` shows the
  packed rows; a cut inside a packed row resumes from that row's first selected label. Plan rows carry
  `packed` and `pack_note`. Library: `pack_labels(data, spec, media) → (stream, packed, note)`, `pack_blocker`.

## 0.16.0 — 2026-10-16

- `printcart` sends ONE stream per printer. The NetPS board resets a NEW connection opened while it still
  prints the previous job; inside one connection there is no such moment — the board takes bytes as its buffer
  drains, TCP blocks the sender, and the chunked send (0.12.1) rides the stall (B29's 1.27 MB took ~2 min that
  way and never failed). So the cart's files for a printer go back to back in one job: the registration prefix
  once, the darkness / speed retuned once in front, then each file in cart order — START, its selected blocks,
  END with `--bookends`. One send per printer; `--dry-run` describes each stream (files, blocks, bookends,
  bytes); `printcart --proof F.pdf|F.png` proofs the composed stream (one document per printer).
- The log gains `stream: {id, offset, length}` per file so the paper, the files and the bytes line up. A cut
  stream is reported file by file: the files before the cut `ok: true`, the one in flight `ok: false` with
  `error: "cut inside F at web row N of M"` (the block holding the first byte the printer did not take) and the
  resume range printed (`--labels N-M`, the operator's call), the files after it `not sent`. Nothing is resent.
- Removed: `--settle` / `"settle"` and the status poll between files (0.14.0 / 0.15.0) — there is no gap between
  files any more. A printer record's `settle` block is read for an old YAML's sake and ignored with a note. The
  ready-wait before a stream's connection stays. A single `print` and a one-file cart are byte-identical to
  0.14.0 (pinned).

## 0.15.0 — 2026-10-16

- The wait between two cart files scales with the job. A flat `--settle` idled three minutes between two one-label
  files and barely covered a 66-row one; measured on the TDP43ME NetPS board, a 66-row job with bookends has its
  END on the roll ~90 s after the send returns (~1.3 s per web row). A printer record may now carry
  `settle: {per_row_s, floor_s}`; `printcart` (and `POST /api/print`, the page) waits `per_row_s × web rows just
  sent (+2 with bookends) + floor_s`, rounded up, counted from the moment the send returned, with no status
  query; the plan prints each file's wait (`then wait 109 s`). `--settle SECONDS` / `"settle"` overrides it
  for every file, `0` disables any wait, and a printer without the record polls its status as in 0.14.0. The
  plan rows carry `rows` and `wait_s`. A single `print` never waits. Older YAML without `settle` is unchanged.

## 0.14.0 — 2026-10-16

- Sends that survive an Ethernet board. On the TDP43ME's NetPS board a cart's second file, sent the instant the
  first was accepted, died with a bare `BrokenPipeError` — the board was still printing the first from its
  buffer, reset the new connection, and then refused port 9100 for ~90 s while it restarted its print server;
  the roll showed the second job's START and nothing behind it, and no log line said how far it got. Now:
  - `send_tcp` waits for the port before the first byte (refused, timed out, closed at once = not ready, backoff
    up to 900 s), retries a connection reset BEFORE any byte was accepted (nothing on the paper), and never one
    reset after (labels are on the paper — a resend prints them twice). Every failure is a `SendError` naming
    host:port and the bytes accepted, never a traceback; `deliver()` returns the note and the bytes.
  - Every send is logged, success or failure, from `print` too (beside the job file, `--log` elsewhere; before,
    only `printcart` logged and a day's single prints had no record): the keys as before plus `ok`, `bytes_sent`
    and `error`; the failure note says whether anything is on the paper; the page lists failed lines in red.
  - Between two cart files the printer is waited for: `--settle SECONDS` / `"settle"` waits with no status
    query (180 recommended for the NetPS board), else `~S,CHECK` is polled until `00`; a printer without status
    gets no wait; the plan states which. A failed send stops the cart (exit 1, API 500 with the plan's `sent` /
    `error` per row).
  - README: *Long jobs on an Ethernet board* — accepted ≠ printed, one connection at a time, the END label as the
    physical idle gate, the port-wedge + self-restart symptom.
- CI: `mypy src` had failed on every push since 0.11.0 (ten errors in cli.py and bookends.py); fixed, and
  `make check` runs ruff, mypy and the tests the way CI does.

## 0.13.0 — 2026-10-16

- `--bookends` on `print` and `printcart`, `"bookends": true` on `POST /api/print`, a checkbox on the page (all
  off by default — nothing existing changes a byte): a START block before and an END block after every file's
  labels, in the SAME send. The roll itself now shows where a job begins and that it completed — the crew held
  the last label of a 128-label job on the roll and asked whether it had finished, and nothing on the paper
  could answer; four more jobs followed on the same roll. The marks are black with hazard-striped edges (no
  cable label looks like that), the word in the largest bold face that fits; START carries the file, `labels
  97-128 of 128`, the count, the printer and the time; END a CUT HERE band, `32/32 sent` and what a missing
  END means. Built on the job's own setup after the selection (never selectable, never counted), they pass the
  same guards, take the printer's heat like the labels, show as the first and last page of `--proof`, and the
  print log gains `"bookends"`. `rasterezpl.bookends`: `bookend_image`, `bookend_blocks`, `with_bookends`.

## 0.12.1 — 2026-10-16

- TCP sends stream in chunks with a stall deadline, like USB: one `sendall` of a 128-label job timed out on the
  TDP43ME NetPS board after part of the job was in the printer; the error names the bytes sent.

## 0.12.0 — 2026-10-16

- `print --proof F.pdf|F.png`: the proof of EVERY web row as it would be sent (after `--labels` / `--first`), one
  PDF page per row at true scale (the page carries the media's dpi — print it on paper at 100 % and lay it over the
  real label) or the rows stacked in one PNG. With `--dry-run` it is the preview of a print that never happens;
  without, the record of one that did. `proof_document(media, data, out)` in `rasterezpl.proof`.

## 0.11.2 — 2026-10-16

- `print --dry-run`: every guard (media match, blocks inside the print areas, the label selection, heat, the
  printer's registration) and the summary line, then nothing is sent — the check `printcart` already had, for one
  file. Asked for on the floor when a `print … --dry-run` was refused as an unknown argument.

## 0.11.1 — 2026-10-15

- The preview follows the form: every change to the composer re-renders the shown row after a short pause
  (debounced, an older render in flight never lands on the page); a *row* field picks which web row to show.
  `preview_png` rasters only that row's labels, not the whole list — a live preview of a 500-row list costs one
  row.
- A column named `n` is data: the template's `{n}` is the row counter only when no column has that name.

## 0.11.0 — 2026-10-15

- The list of labels: a `.csv` / `.tsv` file or a pasted sheet is the page's first-class input — delimiter
  sniffed among tab / comma / semicolon, BOM, CRLF and blank rows tolerated, `header: false` for a plain list
  (`col1`, `col2`, …); the columns appear as buttons that write `{column}` into the template, and the count and
  the first label are shown before anything is composed (`POST /api/rows`).
- A composed job is a recipe: the sidecar keeps `rows` / `template` / `header`, `GET /api/composed` lists the
  jobs newest first, and the page's *recent compositions* reloads one into the form.
- CLI: `text --rows FILE [--template T] [--no-header] [--layout L] [--qr Q]`.

## 0.10.0 — 2026-10-08

- Faded print, two levers: a printer record's optional `darkness` / `speed` are applied to every job at send
  time (`retune`, the file unchanged; `print --darkness/--speed` for one print); `stroke` thickens every glyph
  by that many dots (`render_text`, the composer, the page's *weight*, `text --stroke`).

## 0.9.1 — 2026-10-05

- Layout `barcode-below`: the lines on top, the Code 128 under them — the rack label as Oracle's data halls
  carry it (the first floor read had the two the wrong way round).

## 0.9.0 — 2026-10-05

- Code 128 barcodes, encoded here (ISO/IEC 15417: set B for printable ASCII, set C for runs of four or more
  digits, the checksum, 10-module quiet zones) — `images.code128_image` draws the bars at the widest whole-dot
  module that fits the width and refuses under 2 dots per module. Layouts `barcode` (the bars across the top,
  the lines below — a rack label whose code scans to the text printed under it) and `barcode-only`; they take
  their data through the same `qr` field / template as the QR layouts. The templates sheet and the page show
  them; the test suite round-trips every code through zxing-cpp (a dev dependency).

## 0.8.1 — 2026-10-01

- The clear-laminate layouts take the full-label media themselves: composed on the standard media, the
  composer switches to the stock's `*-full` variant (or refuses clearly when the registry has none), and the
  page switches the media select when such a layout is chosen. On the white-block media the layout had put the
  code on the white and refused the sample text.

## 0.8.0 — 2026-10-01

- Printing on the clear laminate: the `panduit-s150x225vaty-2up-full` preset allows the whole die-cut label;
  layouts `clear-qr` and `clear-image` put the text on the white third and a large QR or picture on the clear
  two thirds. A printer holds a stock: variants at the same dpi (`Media.same_stock`) print on it, so the full
  variant needs no registry change. The demo ships `templates/clear-laminate.ezpl`.
- `match_media` uses the geometry (blocks inside the print areas) and, among variants of one stock, the
  tightest print area (`tightest`): a job on the white block names the standard media, one on the laminate the
  full variant; the 203 vs 300 dpi case resolves by geometry instead of erroring.

## 0.7.0 — 2026-09-30

- QR codes (`images.qr_image`, segno): layouts `badge` (code across the top, lines below — check-in badges),
  `qr-left`, `qr`; the composer's `qr` template filled per label (`{n}`, `{column}` with rows) or `qrs`; a QR
  input on the page; the demo's template sheet shows them.

## 0.6.0 — 2026-09-30

- Pictures on labels (`rasterezpl.images`): PNG / JPEG, or SVG via `cairosvg` (extra `svg`) or `rsvg-convert`;
  fitted and made one-bit by dither or threshold. Layouts `logo` (picture left, text beside), `logo-top`,
  `image`; the composer's `image` / `image_mode` fields; a picture input on the page. The demo draws a
  placeholder mark and shows the asset-tag layouts with it.

## 0.5.2 — 2026-09-30

- `status` on a file-spool printer answers `00 file spool (writable)` instead of raising; an lp: queue says it
  has no status here. The page's *check* works on the demo printers.

## 0.5.1 — 2026-09-30

- The planner tells the same stock at two resolutions apart: a job matches a media only if its blocks lie inside
  that media's print areas, not by the mm header alone. A job composed for the 203 dpi Panduit variant is no
  longer planned onto the 300 dpi printer and refused for geometry; the refusal names the dpi mismatch, and
  a printer holding the right variant is chosen when one exists. The page lists media without a printer last,
  marked.
- `printcart`: a job file given as `--root` is refused with the command that was meant.

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
