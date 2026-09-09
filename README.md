# Sense_tool

A lightweight, standalone clinical-document processing pipeline: upload a
scanned document, OCR it, heuristically pull out sections/tables, and search
or export the result. **v1 has no auth, no multi-tenancy, and no linkage to
patient records** — it's just the intake → OCR → structure → search/export
pipeline.

This is an independent service, not part of the gated Sense /
Medical_Rag_Clinical_Extraction system — no 7-contract model, no stage
discipline.

## Status

The core pipeline (intake → OCR → structure parsing → search → export) is
solid and has been iterated against **real hospital documents**, not just
synthetic fixtures — several rounds of table-extraction and
heading-detection fixes below cite exact evidence (real row/column counts,
before/after section counts) from a real 4-page lab report. All 46+ tests
across the two Python services pass, verified against fresh
`docker compose build --no-cache` rebuilds, not just a cached dev
environment.

Explicitly out of scope for v1, not attempted: **authentication,
multi-tenancy, patient-record linkage** (see above), **S3/cloud object
storage** (local filesystem only, behind a swappable `StorageBackend`
interface — see "Storage abstraction" below), and **any OCR engine besides
Tesseract** (no PaddleOCR fallback, no LayoutParser/Camelot, no spaCy — see
"OCR service" below for why). Known, documented limitations (not bugs):
image-baked text (letterheads, watermarks, any language) is invisible to
native-PDF extraction, and the OCR path defaults to English (opt-in
`lang=eng+guj` / `eng+nep` for Gujarati and Nepali/Devanagari) — see
"Known limitations" under "OCR service" below for the real evidence behind
both.

This repo is a monorepo: four independently-runnable pieces that make up
one pipeline, not four unrelated projects.
- **`app/`** — the main FastAPI service: intake, DB, queue, structure
  parsing, search, export.
- **`ocr_service/`** — a standalone OCR microservice. It's separate because
  it's the one step with heavier, more likely-to-change native dependencies
  (Tesseract/OpenCV today, maybe another engine later); the main app just
  calls it over HTTP (`app/services/ocr.py`).
- **`sense_tool_ui/`** — a minimal Next.js internal testing UI for the
  pipeline above (upload, browse, search). See
  [sense_tool_ui/README.md](sense_tool_ui/README.md).
- **`ocr_service_eval/`** — a standalone, non-pytest accuracy-evaluation
  harness you run by hand against real sample documents (never committed -
  see its README's sample-data policy). See
  [ocr_service_eval/README.md](ocr_service_eval/README.md).

## Stack

| Concern         | Choice                                             |
|------------------|-----------------------------------------------------|
| API              | FastAPI + Pydantic v2                               |
| Queue            | Arq (Redis-backed, async-native)                    |
| OCR              | Separate service (see below): pdfplumber native-PDF-text-first (+ table extraction), else OpenCV preprocessing + Tesseract (LSTM; English default, opt-in `eng+guj` / `eng+nep` multi-script) |
| Database         | PostgreSQL, SQLAlchemy 2.0 (async) + Alembic         |
| Object storage   | Local filesystem, behind a `StorageBackend` interface |
| Export           | PDF: native-PDF pass-through, or a Tesseract searchable PDF for scans (WP-G). DOCX/XLSX: `python-docx` / `openpyxl` structured recreation + `pillow` image embedding. `pypdf` merges searchable-PDF pages |
| Search           | Postgres full-text search (generated `tsvector` + GIN index) |

## Data model

A single `documents` table (migrations `0001` create, `0002` add
`image_regions`):

```
id                UUID PK
source            varchar        -- e.g. hospital/clinic name
status            enum           -- QUEUED | PROCESSING | DONE | FAILED
raw_file_path     varchar        -- storage locator (see app/storage)
extracted_text    text, nullable
structured_data   jsonb, nullable -- {"sections": [...], "tables": [...]};
                                 -- table entries have >1 shape - branch on
                                 -- `source` (native_pdf grid | ruled_line_region
                                 --  text blob | heuristic grid)
image_regions     jsonb, nullable -- WP-B: [{bbox, page, source, region_type_guess,
                                 --         storage_key, ...}]; crop bytes live in
                                 --         storage, not the DB. Not consumed by
                                 --         structure parsing or export yet.
error_message     text, nullable
search_vector     tsvector, generated from extracted_text (GIN-indexed)
created_at        timestamptz
updated_at        timestamptz
```

## Pipeline

```
POST /documents/intake
   -> save upload to storage, create Document(status=QUEUED)
   -> enqueue Arq job "run_ocr_extraction"
   -> return {id, status} immediately (202, no blocking on OCR)

Arq worker: run_ocr_extraction
   -> status = PROCESSING
   -> read file bytes from storage, POST to the OCR service -> extracted_text
      (+ WP-B: extracted image/photo/chart regions -> crop bytes to storage
       under {id}/images/, metadata to Document.image_regions; not used by
       structure parsing or export yet)
   -> enqueue "run_structure_parsing"
   -> on any failure: status = FAILED, error_message set, job stops

Arq worker: run_structure_parsing
   -> split extracted_text into sections; tables come from the OCR service
      (native-PDF grids, or WP-D ruled-line regions on the scanned path)
   -> Document.structured_data set, status = DONE
   -> on any failure: status = FAILED, error_message set
```

A document is never left stuck in `PROCESSING` — every failure path sets
`FAILED` with a recorded `error_message` (see [app/worker.py](app/worker.py)).

Section parsing is intentionally simple (v1): a short ALL-CAPS line or a
line ending in `:` starts a new section.

**Table extraction depends on the path, and `structured_data["tables"]`
entries have more than one shape — a consumer must branch on `source`:**

- **Native-PDF path** — `ocr_service` calls `page.extract_tables()` on the
  same pdfplumber page objects used for text (needed because native-PDF
  text collapses a table's column spacing to single spaces, so no
  space-based detector can see it — confirmed on a real 4-page lab report
  where a `Test Name | Status | Result | …` results table came back as 0
  detected tables). Entries: `{raw_lines, rows: list[list[str]], source:
  "native_pdf"}` — a real grid. Malformed (ragged/empty) tables are
  dropped, not fatal.
- **Rasterize+OCR path (WP-D)** — the old space-alignment heuristic was
  measured (WP-C/WP-D) as pure garbage on scanned pages, so it is
  **disabled** here. Instead `ocr_service`'s ruled-line detector
  (`ocr_service/app/tables.py`) finds ruled-table bounding boxes and OCRs
  each as one block. Entries: `{bbox, region_text: str, source:
  "ruled_line_region"}` — **a text blob, no grid**. Borderless
  whitespace-aligned tables are not detected (known gap); their lines stay
  in `text` / section content.
- The space-alignment heuristic (`source: "heuristic"`) now only runs for
  native-PDF text that produced no `extract_tables()` grids — where its own
  history shows it "never fires" — kept as a harmless no-op, not removed.

Table data (native grids or ruled-line regions) and a `text_source` flag
ride the job chain from `run_ocr_extraction` to `run_structure_parsing` as
arq job arguments (transient, not DB columns). See
[app/services/structure.py](app/services/structure.py),
[ocr_service/app/ocr.py](ocr_service/app/ocr.py) and
[ocr_service/app/tables.py](ocr_service/app/tables.py).

**False-positive filtering.** `extract_tables()`'s default detection is
liberal: on that same real lab report it also misdetected 2 pure-paragraph
blocks (an HRCT findings section, a disclaimer) as 1-column "tables" -
they're bounded by decorative rules/a border rect, which is enough
structure for pdfplumber to guess a table, but they're not one. Checked
`page.lines`/`.rects`/`.curves` before reaching for a fix: the genuine
table's grid is built from rects/curves, not stroked line objects, so
`vertical_strategy="lines_strict"` finds *zero* tables anywhere on the
document, including the real one - not usable. Raising `edge_min_length`
(tested up to 200) never removed the false positives either (the page's
own content-box rect is long enough to qualify at any tested length)
while it started dropping real columns from the genuine table past ~150.
Column count is the signal that actually discriminates in this document -
every false positive collapsed to exactly 1 column, every genuine table
had 5 - so `ocr_service` (and defensively, `structure.py` again on the
other side of the HTTP boundary) requires ≥2 rows and ≥2 columns for a
detected *table* to be kept. Result on that document: 4 tables → 2, both
genuinely tabular (the results grid spans pages 1-2). Separately confirmed:
a table drawn with only horizontal rules and no vertical dividers already
returns zero tables from `extract_tables()`'s default strategy *before*
any of this filtering runs - a pre-existing recall limit this change
didn't introduce and doesn't fix. See the comment above
`ocr_service/app/ocr.py::_MIN_TABLE_ROWS` for the full investigation.

**Row-level filtering.** The table-level filter above doesn't catch a
defect *inside* an otherwise-genuine table: page 2's surviving table still
had a spurious row 0 - `['Lab No.: ... UHID: ...\nPatient Name: ...
\n...\nDoctor: ...', None, None, None, None]` - the page's repeated
patient-info letterhead, landing in the table because its column dividers
don't cross that row's y-range. Before writing a rule, checked whether any
*genuine* row has the same "only one populated cell" shape (since that
alone isn't safe to drop on): yes - `Differential Leucocyte Count`, alone
in column 0 on the *other* table, is a real section-subheading row (the
Neutrophil/Lymphocyte/etc. rows are its breakdown), not noise. The
distinguishing signal between those two real examples: the letterhead cell
has 4 embedded newlines and 266 characters; the subheading cell has 0
newlines and 28. So the rule (in both `ocr_service` and `structure.py`) is:
a row with exactly one non-empty cell is dropped only when that cell's
text spans multiple lines; a short single-line label is kept, and a row
with ≥2 non-empty cells is never touched regardless of how many *other*
cells are empty (most genuine rows here have blank Status/Reference
columns). Result: table 1's letterhead row is gone (12 rows → 11, all
genuine data intact); `Differential Leucocyte Count` survives untouched in
table 0 (which also dropped one fully-blank trailing row - a separate,
uncontroversial case this same rule catches, not data loss). Honestly: the
"any newline" threshold is the only one this document's two real examples
actually calibrate (0 vs 4) - a genuinely long single-cell row that
happens to soft-wrap onto a second line would also be caught by this rule,
and there's no evidence here to justify a more lenient bar. See the
comment above `ocr_service/app/ocr.py::_is_spurious_row`.

**Heading false-positives in the flat-text/sections path.** The three
fixes above are about `structured_data["tables"]`; the same document's
`structured_data["sections"]` had a separate, unrelated bug: the
all-caps-line heading rule ran unconditionally on every line, including
ones already captured by the native tables above (the flat text and the
tables are computed independently, so this content is duplicated, not
exclusive to one or the other). On this document that turned 26 "sections"
out of what should've been 5: short method-name annotations that sit on
their own line directly under a result line (`COLORIMETRIC` right after
`Haemoglobin (Hb) * L 11.4* 12.0-15.0 g/dL`, `CALCULATED` seven separate
times, `ELECTRICAL IMPEDENCE`, `FLOW CYTOMETRY`) each became their own
0-content section, and so did a couple of result rows whose own test-name
abbreviation happens to be all-uppercase (`RDW-CV H 15.7* 11.5-14.5 %`,
`ATYPICAL CELLS 00`). The distinguishing signal, checked against this
document rather than assumed: none of the false positives are digit-free
in isolation - the method-name lines have no digit themselves but always
immediately follow a line that does, and the stray data rows carry a digit
in their own result value. Genuine headings on the same document
(`COMPLETE HEMOGRAM`, `HRCT – CHEST`, `FINDINGS:`) are each digit-free
themselves *and* preceded by a digit-free line. So `_is_header_line` now
also takes the previous line and rejects the line as a heading if either
it or the line before it contains a digit - reset on blank lines, so the
signal can't leak across a paragraph/page break and suppress a genuine
heading that happens to open the next block. Result: 26 sections → 5, and
the method-name lines now read as part of the test-name content right
above them (e.g. `Haemoglobin (Hb) * L 11.4* 12.0-15.0 g/dL\nCOLORIMETRIC`
in one section body) rather than fragmenting into disconnected sections.
Not evidenced either way: a genuine heading that happens to contain a
digit (e.g. `COVID-19 SCREENING`) would also be suppressed by this rule -
no such case exists in the document this was checked against. See the
comment above `app/services/structure.py::_is_header_line`.

## OCR service

`ocr_service/` is a small standalone FastAPI app (own Dockerfile, own
`requirements.txt`) exposing:

- `POST /ocr?psm=<0-13>&lang=<eng|eng+guj|eng+nep|…>&extract_images=<bool>` —
  multipart `file`. For a PDF, it first tries native text extraction via
  **pdfplumber** — if the PDF already has a real text layer (i.e. it isn't
  just a scanned image), that text (plus any tables found via
  `page.extract_tables()` on the same pages - see below) is returned
  directly and Tesseract never runs. Otherwise (PDFs with no text layer,
  and all images) it rasterizes if needed, runs OpenCV preprocessing
  (grayscale → Otsu binarization → deskew), then Tesseract 5 in LSTM-only
  mode (`--oem 1`, default `--psm 6`, overridable per request). `lang`
  selects the Tesseract language(s): default `eng`; `eng+guj` / `eng+nep`
  (or any `+`-joined combination of installed codes) enable **single-pass
  multi-script OCR** for Gujarati and Nepali/Devanagari. Omitting `lang` is
  byte-for-byte the old English-only path. Response:
  `{"text": str, "text_source": "native_pdf"|"ocr", "tables": [...],
  "table_regions": [...], "images": [...]}`. (WP-G's searchable PDF is a
  **separate** endpoint, `POST /searchable-pdf`, called by its own
  follow-up job — `/ocr` has no searchable-PDF coupling; see "PDF (WP-G)"
  under Export and `ocr_service/README.md`.)
  `tables` is pdfplumber's raw per-table row/cell **grid** — native-PDF
  path only, `[]` otherwise. `table_regions` (WP-D) is the rasterize+OCR
  path's ruled-line table **regions** — `{bbox, region_text, source:
  "ruled_line_region"}`, a text blob with **no grid** — `[]` on the native
  path; borderless whitespace-aligned tables are not detected.
  `images` (WP-B) is embedded raster images on the native path and detected
  photo/chart/logo/stamp region crops on the rasterize+OCR path, each with
  a bbox, a base64 crop, and a low-confidence `region_type_guess`;
  `extract_images=false` skips it. See
  [ocr_service/README.md](ocr_service/README.md) for the two table paths,
  the two table shapes, and the detectors' honest measured accuracy. Text
  extraction is unchanged regardless of the table/image options.
- `GET /health`.

This is deliberately Tesseract-only for v1 — no PaddleOCR fallback (WP-C
evaluated PP-Structure: Apache-2.0 but ~2.8 GB of deps+weights and it
**crashes on this CPU stack** — not adopted), no LayoutParser/Camelot, no
spaCy post-processing. Table structure on the scanned path is
classical-CV only (`ocr_service/app/tables.py`, WP-D): morphological
ruled-line detection + whole-region OCR, no new dependencies. Heavier
engines remain a v2 option if real documents prove it insufficient, but
aren't worth the dependency weight speculatively.

**Native-PDF-text library note:** this used PyMuPDF (`fitz`) initially, but
PyMuPDF is AGPLv3-licensed and Sense_tool has had no legal review clearing
AGPLv3 for production/commercial use — so it was replaced with
**pdfplumber** (MIT), which covers the same "does this PDF already have a
text layer" check. Test fixtures that need to *generate* a PDF (not extract
from one) use `reportlab` (BSD), dev-only.

See [ocr_service/app/ocr.py](ocr_service/app/ocr.py) and
[ocr_service/app/preprocess.py](ocr_service/app/preprocess.py).

### Known limitations

- **Native-PDF-text extraction only sees the PDF's live text layer.** Any
  content baked into an image — letterheads, watermarks, stamped/signature
  text, decorative banners, or text in any language rendered as a graphic
  rather than selectable text — is invisible to this path, regardless of
  language. Confirmed on a real sample document: a hospital letterhead's
  Hindi/Devanagari prayer section turned out to be a raster image with no
  text layer at all (checked via font inspection — no Devanagari-capable
  font was used anywhere on the page — and by extracting and viewing the
  image itself). Not an encoding bug, not language filtering — a
  source-PDF authoring characteristic: the letterhead was designed as a
  graphic asset, not typed text.

- **The rasterize+OCR path defaults to English, with opt-in multi-script.**
  With `lang` omitted it runs Tesseract `-l eng` only — a
  Hindi/Devanagari, Gujarati, or any other non-English document sent
  through OCR at that default produces Latin garbage or nothing. Passing
  `lang=eng+guj` or `lang=eng+nep` enables Gujarati / Nepali-Devanagari
  recognition in the same pass (tessdata for `eng`/`guj`/`nep` is baked
  into the image; see `ocr_service/app/ocr.py::_tesseract_config` and
  `ocr_service/Dockerfile`). It's opt-in per request, not the default —
  see `ocr_service/README.md` ("Accuracy tradeoff") for the measured
  reason. Other scripts still need their own language pack added. This is a
  different limitation from the one above (OCR accuracy vs. images having
  no text layer at all).

  A workaround for the *native-PDF image-content* case (script text baked
  into a letterhead graphic, invisible to pdfplumber) is still deferred,
  not built: OCR the image regions specifically (via `page.images`
  bounding boxes) with the right language pack. This needs image-region
  classification first — a page's images aren't all "banner text" (one
  real letterhead also has two unrelated portrait photos), so OCR-ing
  every image blindly would produce garbage on the non-text ones.

### OCR service call timeout

`app/services/ocr.py` sets an explicit per-phase `httpx.Timeout` on the call
to the OCR service (`connect=5s`, `write=10s`, `pool=5s`,
`read=240s` — configurable via `OCR_*_TIMEOUT_SECONDS` env vars, see
[app/config.py](app/config.py)).

The `read` value is set from a real measurement, not a guess: a 25-page,
300 DPI scanned PDF (dense text, no embedded text layer — the worst case,
since it can't take the native-PDF shortcut) took **~102s** end-to-end
against this service's Tesseract pipeline running in Docker (~4.1s/page,
confirmed linear against a second, 10-page/~40s run). The originally-assumed
90s default doesn't clear that real 102s file, so `read` is set to 240s
instead — comfortable margin over the measured case (covers ~58 pages at
the measured rate) for the largest realistic multi-page scanned document.

If the OCR service call exceeds `read`, `httpx.TimeoutException` propagates
out of `app/services/ocr.py` uncaught, and `run_ocr_extraction`
([app/worker.py](app/worker.py)) catches it specifically (before the
general failure handler) and sets `status=FAILED` with
`error_message="OCR service timed out after {N}s"` — never left stuck in
`PROCESSING`. Covered by
`tests/test_pipeline.py::test_ocr_timeout_marks_document_failed_with_clear_message`.

## Export

`GET /documents/{id}/export?format=pdf|docx|xlsx`.

**Two philosophies, by format:**

| format | what you get |
|---|---|
| **`pdf`** (WP-G) | **Visual fidelity.** Native-PDF documents export as a byte-for-byte pass-through of the uploaded original (exact fonts, layout, images, tables — nothing is reconstructed). Scanned/OCR documents export as a **Tesseract searchable PDF**: the original page image(s) with an invisible, word-positioned OCR text layer, so the visual *is* the original and the text is now selectable/searchable. |
| **`docx` / `xlsx`** (WP-F) | **Best-effort structured recreation** from `structured_data` + the WP-B images ([app/services/export.py](app/services/export.py)) — *not* an exact-layout clone. |

### PDF (WP-G) — pixel-perfect

The right output for `format=pdf` is chosen from `Document.text_source`
(persisted by the OCR worker step, migration `0003`):

| `text_source` | PDF export | how |
|---|---|---|
| `native_pdf` | the uploaded file, streamed verbatim | the source PDF already *is* the exact original layout — no rebuild. `_pixel_perfect_pdf` in [app/routers/documents.py](app/routers/documents.py) reads `raw_file_path` from storage and returns it as-is. |
| `ocr` | a pre-generated Tesseract searchable PDF | built by a **separate follow-up job** after OCR (see below), from the **original** page image(s) + an invisible OCR text layer, stored at `searchable_pdf_key`, streamed on export. WP-B extracted images need no separate embedding — they are already in the page raster. If the job hasn't finished or failed, `searchable_pdf_key` is `NULL` and export falls back. |
| `NULL` (pre-WP-G rows, a missing original, or a not-yet/failed searchable-PDF job) | the legacy structured-text PDF (`render_pdf`) | a plain flatten of `structured_data` — kept only as a fallback. |

**Two separate Tesseract passes — and why.** An A/B evaluation on 7 real
scanned documents (DEXA printout photo, Gulf Gujarati/Nepali forms,
workplace lab report, a heavily skewed photo, two digital DEXA renders)
compared the tuned primary pass against a single un-preprocessed pass and
found the un-preprocessed one **measurably worse on real scans**: a lab
report lost its units and reference-range columns, a DEXA printout lost its
results-table row/value association, and a skewed photo collapsed to two
tokens — while reporting *higher* confidence (fewer, easier words). So:

- **Primary path — unchanged.** `run_ocr_extraction` still feeds
  `Document.extracted_text` / structure / search from the OpenCV
  Otsu/deskew + `--oem 1 --psm 6` pass, exactly as before WP-G. It carries
  no searchable-PDF coupling at all.
- **Searchable-PDF path — new, async, isolated.** `run_ocr_extraction`
  enqueues a follow-up Arq job, `run_searchable_pdf_generation`, only for
  `text_source == "ocr"`. It calls the OCR service's dedicated
  `POST /searchable-pdf` ([ocr_service/app/searchable_pdf.py](ocr_service/app/searchable_pdf.py)),
  which runs **its own** Tesseract pass per page via `run_tesseract` with
  `--oem 1 --psm 6` (pinned like the primary path — `run_and_get_multiple_output`
  takes no config, so it isn't used) on the **un-preprocessed** original
  image. Tesseract always renders whatever it OCRs as the PDF's visible
  layer, so this pass *must* see the original; pinning psm keeps the
  invisible text layer's page segmentation sane for tabular scans. The
  deskew/binarise difference from the primary path is the one unavoidable
  gap, and it only affects the invisible layer, never `extracted_text`.
- **Failure is a non-event.** Any failure in the follow-up job (or its
  960s job timeout) is logged and swallowed — `extracted_text`, `status`,
  `structured_data` and export are untouched; `searchable_pdf_key` stays
  `NULL` and `format=pdf` serves the structured-text fallback.

Multi-page scanned PDFs are re-rasterised (poppler) and the per-page PDFs
merged in page order with `pypdf`. `lang` is threaded from
`run_ocr_extraction` through the job to `/searchable-pdf` (and persisted as
`Document.ocr_lang`), so an `eng+guj` / `eng+nep` run produces a correct
**Unicode** invisible layer in the matching script — verified in the
OCR-service test suite, not assumed.

### DOCX / XLSX (WP-F) — structured recreation

**Tables — two shapes, branched on `source`** (see the "Table extraction"
note above):

| `source` | DOCX | XLSX |
|---|---|---|
| `native_pdf` / `heuristic` (a real grid) | a real Word table, cells as extracted | real cells on the `Tables` sheet |
| `ruled_line_region` (a text blob, no grid) | `Table N (detected, unstructured)` heading + an italic note + the `region_text` as a plain paragraph — **no fake table object** | a bold label row + the whole `region_text` in one merged, wrapped cell — **no fake column split** |

**Images** — *all* `image_regions` are embedded (WP-B principle: a silently
dropped image is worse than a low-value one; WP-B reported no "known-noise"
guess class to exclude). `region_type_guess` is used only as a caption hint
(`Figure N (page P, embedded|detected: photo|chart|logo|stamp|unknown)`),
never as a filter. Ordering is by `(page, bbox top-y)` — reasonable reading
order, not exact position.

- **DOCX**: a `Figures` section, each image inline under its caption, width
  capped to fit US-Letter margins. An image python-docx's strict header
  parser rejects (some valid embedded-PDF JPEGs — e.g. LabReport-1.pdf's
  signature stamp) is re-encoded via Pillow and retried before any
  `[could not embed]` fallback.
- **XLSX**: a dedicated `Figures` sheet (one labelled block per image) —
  anchoring images next to their content is impractical in XLSX's grid
  model, so a summary sheet is used deliberately, not as a fallback.

(For a visually faithful copy of the source, use `format=pdf` — WP-G.)

`pillow` is a runtime dependency (openpyxl needs it to embed images).
Verified end-to-end on LabReport-1.pdf (2 native grids + all 6 embedded
images) and on the DEXA-scanned / Gulf-Gujarati `ruled_line_region`
samples (text-blob tables render as labelled text, detected-image crops
embed intact); a document with no tables and no images exports with no
crash and no empty/broken elements.

## Endpoints

- `POST /documents/intake` — multipart form: `source` (string), `file`
  (upload). Returns `202` with `{id, status, source}`.
- `GET /documents/{id}` — full record, including `extracted_text`,
  `structured_data`, and `image_regions` (WP-B image/photo/chart region
  metadata; crop bytes are in storage at each entry's `storage_key`).
- `GET /documents/search?q=...&limit=&offset=` — Postgres full-text search
  over `extracted_text`, ranked by `ts_rank`, with a highlighted snippet per
  result.
- `GET /documents/{id}/export?format=pdf|docx|xlsx` — `pdf` is visual-
  fidelity (WP-G): native-PDF pass-through, or a Tesseract searchable PDF
  for scans. `docx` / `xlsx` are a best-effort structured recreation of
  `structured_data` **and the WP-B extracted images**. `409` if the
  document isn't `DONE` yet. See "Export" below.
- `GET /health` — liveness check.

Interactive API docs: `http://localhost:8000/docs`.

## Running with Docker (recommended)

```bash
cp .env.example .env
docker compose up --build
```

This starts `postgres`, `redis`, and `ocr` (port 8001), runs
`alembic upgrade head` once (`migrate` service, waits on `ocr`/`postgres`
being healthy), then starts the `api` (port 8000) and `worker` containers.
Uploaded files are stored in a named volume mounted at `/data/storage` in
both `api` and `worker`.

```bash
curl -F "source=Riverside Clinic" -F "file=@scan.png" \
  http://localhost:8000/documents/intake

curl http://localhost:8000/documents/<id>

curl "http://localhost:8000/documents/search?q=bronchitis"

curl -o report.pdf "http://localhost:8000/documents/<id>/export?format=pdf"
```

## Running locally (without Docker)

The main app (`app/`) has no native OCR dependencies itself, but the OCR
service (`ocr_service/`) needs local `tesseract-ocr` and `poppler-utils`
(for PDF rasterization) binaries. For multi-script OCR also install the
`tesseract-ocr-guj` / `tesseract-ocr-nep` language packs (and, to run the
multi-script tests, the `fonts-lohit-gujr` / `fonts-lohit-deva` fonts) —
the Docker image bakes all of these in already. Plus a running Postgres
and Redis.

```bash
# main app
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # includes requirements.txt + pytest

# ocr service - separate venv, separate deps
cd ocr_service && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && cd ..

docker compose up -d postgres redis   # or run your own Postgres/Redis

cp .env.example .env
# then point DATABASE_URL / DATABASE_URL_SYNC / REDIS_URL / OCR_SERVICE_URL
# at localhost instead of the docker-compose service names, e.g.:
#   DATABASE_URL=postgresql+asyncpg://sense_tool:sense_tool@localhost:5432/sense_tool
#   DATABASE_URL_SYNC=postgresql+psycopg2://sense_tool:sense_tool@localhost:5432/sense_tool
#   REDIS_URL=redis://localhost:6379/0
#   OCR_SERVICE_URL=http://localhost:8001

alembic upgrade head

(cd ocr_service && source .venv/bin/activate && \
  uvicorn app.main:app --reload --port 8001)   # terminal 1
source .venv/bin/activate && uvicorn app.main:app --reload   # terminal 2
source .venv/bin/activate && arq app.worker.WorkerSettings   # terminal 3
```

## Migrations

```bash
alembic upgrade head        # apply
alembic downgrade base      # roll back
alembic revision -m "..."   # new migration (autogenerate needs a live DB:
                             # alembic revision --autogenerate -m "...")
```

`alembic/env.py` reads `ALEMBIC_DATABASE_URL` if set, otherwise
`Settings.database_url_sync` (a plain `psycopg2` URL — separate from the
app's async `asyncpg` URL, since Alembic runs synchronously).

## Tests

Two independent test suites, each with its own venv/requirements:

**Main app** (`tests/`) runs against a **real** Postgres + Redis (no mocks
for the DB/queue layer — Postgres full-text search and Arq's real
enqueue/dequeue path are exercised directly). The OCR *service call* is
monkeypatched at the `app.worker.ocr.extract_text` boundary (an async
function) rather than run for real, so this suite doesn't depend on the OCR
service or Tesseract being available at all.

```bash
docker compose up -d postgres redis
cp .env.example .env.local
# edit .env.local to point at localhost (see "Running locally" above)
set -a && source .env.local && set +a
pytest
```

Coverage includes:
- `tests/test_intake.py` — upload validation, storage, DB row creation, job
  enqueue, `GET /documents/{id}`.
- `tests/test_pipeline.py` — `run_ocr_extraction` / `run_structure_parsing`
  status transitions, the **FAILED path** for both steps, the
  never-stuck-in-PROCESSING guarantee, and (WP-G) that the OCR step
  persists `text_source` / `ocr_lang` and enqueues a separate
  `run_searchable_pdf_generation` job (OCR path only), that the job stores
  `searchable_pdf_key`, threads `lang`, and that any failure in it is fully
  isolated — no change to status / `extracted_text` / export.
- `tests/test_end_to_end.py` — a real `arq.worker.Worker` (burst mode)
  draining the actual Redis queue after a real `POST /documents/intake`,
  both for the happy path and the OCR-failure path.
- `tests/test_search.py` — full-text search ranking/snippets/empty results.
- `tests/test_export.py` — export endpoint: `404`/`409`/`422` handling,
  image crops flowing from storage into the generated docx, and the WP-G
  PDF branches — native-PDF byte-for-byte pass-through, `ocr` docs
  streaming the pre-built searchable PDF, and the fallback to the
  structured-text PDF when `text_source` is `NULL` or the artifact is
  missing.
- `tests/test_export_render.py` — pure renderers: native-grid → real Word
  table / real cells; `ruled_line_region` → labelled text (never a fake
  table); all images embedded with caption hints; empty document exports
  cleanly; a bad image crop degrades to a note, not a crash.
- `tests/test_structure_service.py` — heuristic section/table parsing, plus
  the native-PDF-table hybrid path (uses `native_tables` when given,
  falls back to the heuristic when not, drops malformed tables).
- `tests/test_migration.py` — the Alembic migration applies, round-trips
  (`upgrade` → `downgrade` → `upgrade`), against a scratch database.

**OCR service** (`ocr_service/tests/`) is self-contained — no Postgres/Redis
needed:

```bash
cd ocr_service && source .venv/bin/activate && pytest
```

The native-PDF-text tests (including table extraction and WP-B embedded
image extraction) run anywhere (only need `pdfplumber` + `reportlab`, no
Tesseract). The tests that actually invoke Tesseract auto-skip if a
`tesseract` binary isn't on `PATH` — that includes the Gujarati/Nepali
multi-script tests (which additionally skip without the `guj`/`nep`
tessdata and a matching Lohit font), the WP-B scanned-page region
detection tests, and the WP-G searchable-PDF tests (searchable PDF from a
scanned image / multi-page order preservation / `null` on the native path
/ correct Unicode invisible layer for `eng+guj` and `eng+nep`). All of
them run for real inside the `ocr` Docker image, where the full suite is
green.

## Storage abstraction

`app/storage/base.py` defines `StorageBackend` (`save`, `read`,
`resolve_local_path`, `exists`). `app/storage/local.py` is the only
implementation today (files under `STORAGE_DIR`). To add S3-compatible
storage later, implement the same interface and swap the factory in
`app/storage/__init__.py::get_storage_backend` — no caller changes needed.

## Configuration

See [.env.example](.env.example). All settings are read via
`app/config.py` (`pydantic-settings`), overridable by environment variables
or a `.env` file.

## License

[MIT](LICENSE).
