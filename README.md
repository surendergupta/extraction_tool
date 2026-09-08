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
native-PDF extraction, and the OCR fallback path is English-only — see
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
| OCR              | Separate service (see below): pdfplumber native-PDF-text-first (+ table extraction), else OpenCV preprocessing + Tesseract (LSTM, English) |
| Database         | PostgreSQL, SQLAlchemy 2.0 (async) + Alembic         |
| Object storage   | Local filesystem, behind a `StorageBackend` interface |
| Export           | `pypdf`, `python-docx`, `openpyxl`                   |
| Search           | Postgres full-text search (generated `tsvector` + GIN index) |

## Data model

A single `documents` table (see [alembic/versions/0001_create_documents_table.py](alembic/versions/0001_create_documents_table.py)):

```
id                UUID PK
source            varchar        -- e.g. hospital/clinic name
status            enum           -- QUEUED | PROCESSING | DONE | FAILED
raw_file_path     varchar        -- storage locator (see app/storage)
extracted_text    text, nullable
structured_data   jsonb, nullable -- {"sections": [...], "tables": [...]}
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
   -> enqueue "run_structure_parsing"
   -> on any failure: status = FAILED, error_message set, job stops

Arq worker: run_structure_parsing
   -> heuristically split extracted_text into sections/tables
   -> Document.structured_data set, status = DONE
   -> on any failure: status = FAILED, error_message set
```

A document is never left stuck in `PROCESSING` — every failure path sets
`FAILED` with a recorded `error_message` (see [app/worker.py](app/worker.py)).

Structure parsing is intentionally simple (v1): a short ALL-CAPS line or a
line ending in `:` starts a new section; consecutive lines that look
column-aligned (pipe, tab, or 2+ spaces) become a table block.

**Native-PDF tables are the one exception.** Native-PDF text extraction
collapses a table's visual column spacing into single spaces (`pdfplumber`'s
`.extract_text()`), so that space-heuristic never fires on a real table in a
native-PDF document — confirmed on a real 4-page lab report where a results
table (`Test Name | Status | Result | Reference Interval | Unit`) came back
as 0 detected tables. For that path only, `ocr_service` also calls
`page.extract_tables()` on the same pdfplumber page objects (not discarded
once text is pulled) and hands the raw table data through the job chain
(`run_ocr_extraction` → `run_structure_parsing`, as an extra arq job
argument, not a DB column - it's only needed transiently between those two
steps). When present, `parse_structure` uses it directly for that
document's `tables` (tagged `"source": "native_pdf"` vs `"heuristic"`)
instead of running the space-heuristic detector, skipping malformed
(ragged/empty) tables rather than failing the document. The OCR/rasterized
path has no page object to call `extract_tables()` on and is completely
unaffected - it always uses the heuristic, unchanged. See
[app/services/structure.py](app/services/structure.py) and
[ocr_service/app/ocr.py](ocr_service/app/ocr.py).

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

- `POST /ocr?psm=<0-13>` — multipart `file`. For a PDF, it first tries
  native text extraction via **pdfplumber** — if the PDF already has a real
  text layer (i.e. it isn't just a scanned image), that text (plus any
  tables found via `page.extract_tables()` on the same pages - see below)
  is returned directly and Tesseract never runs. Otherwise (PDFs with no
  text layer, and all images) it rasterizes if needed, runs OpenCV
  preprocessing (grayscale → Otsu binarization → deskew), then Tesseract 5
  in LSTM-only mode (`--oem 1`, English, default `--psm 6`, overridable per
  request). Response: `{"text": str, "tables": [...]}` - `tables` is
  pdfplumber's raw per-table row/cell shape, always `[]` outside the
  native-PDF-text path.
- `GET /health`.

This is deliberately English-only and Tesseract-only for v1 — no PaddleOCR
fallback, no LayoutParser/Camelot table detection (pdfplumber's own
`extract_tables()` on the native-PDF path is much lighter-weight than
either and already shipped - see "Structure parsing" above), no spaCy
post-processing. Those remain real options for a v2 if real documents prove
insufficient, but they're not worth the dependency weight (and, for
LayoutParser/detectron2, the CPU-only Docker build pain) speculatively.

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

- **The rasterize+OCR fallback path is English-only**, separately from the
  above: it runs Tesseract with `-l eng` only (see
  `ocr_service/app/ocr.py::_tesseract_config`). Even a document that *did*
  go through OCR instead of native extraction would not have its
  Hindi/Devanagari (or any other non-English) text correctly recognized —
  Tesseract would read it with the English model and produce garbage or
  nothing, not real text. This is a different limitation from the one
  above (OCR accuracy vs. images having no text at all) and would need its
  own fix (a non-English language pack) if it mattered.

  A workaround for the image-content case is deferred, not built: OCR the
  image regions specifically (via `page.images` bounding boxes) with a
  Hindi language pack. This needs image-region classification first — a
  page's images aren't all "banner text" (that same letterhead also has
  two unrelated portrait photos), so OCR-ing every image blindly would
  produce garbage on the non-text ones.

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

## Endpoints

- `POST /documents/intake` — multipart form: `source` (string), `file`
  (upload). Returns `202` with `{id, status, source}`.
- `GET /documents/{id}` — full record, including `extracted_text` and
  `structured_data`.
- `GET /documents/search?q=...&limit=&offset=` — Postgres full-text search
  over `extracted_text`, ranked by `ts_rank`, with a highlighted snippet per
  result.
- `GET /documents/{id}/export?format=pdf|docx|xlsx` — renders
  `structured_data` into the requested format. `409` if the document isn't
  `DONE` yet.
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
(for PDF rasterization) binaries. Plus a running Postgres and Redis.

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
  status transitions, the **FAILED path** for both steps, and the
  never-stuck-in-PROCESSING guarantee.
- `tests/test_end_to_end.py` — a real `arq.worker.Worker` (burst mode)
  draining the actual Redis queue after a real `POST /documents/intake`,
  both for the happy path and the OCR-failure path.
- `tests/test_search.py` — full-text search ranking/snippets/empty results.
- `tests/test_export.py` — pdf/docx/xlsx rendering, `404`/`409`/`422`
  handling.
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

The native-PDF-text tests (including table extraction) run anywhere (only
need `pdfplumber` + `reportlab`, no Tesseract). The two tests that actually
invoke Tesseract auto-skip if a `tesseract` binary isn't on `PATH`, and run
for real inside the `ocr` Docker image.

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
