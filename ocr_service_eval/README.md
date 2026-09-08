# ocr_service evaluation harness

A standalone script for manually measuring OCR accuracy/behavior against
real hospital sample documents. **This is not a pytest suite** — nothing
here runs in CI or asserts pass/fail; a human runs it by hand and reads the
report.

It talks to the real, already-running `ocr_service` only over its public
`POST /ocr` HTTP endpoint — exactly like any production client. It never
modifies `ocr_service`'s code or behavior.

## ⚠️ Sample data policy

**Sample documents must live outside this repo entirely — not just
gitignored, not even in a gitignored folder inside `sense_tool/`.**

Why "outside the repo entirely" and not "gitignored is enough": these are
real hospital documents. If one ever gets `git add`ed by mistake (a
gitignore rule can be wrong, overridden with `-f`, or simply not cover a
new filename pattern someone introduces later), it's now in the repo's
history permanently — `git rm` doesn't remove it from prior commits, and
neither does adding a `.gitignore` rule after the fact. The consent
covering this data is scoped to *testing this pipeline*, not to *living
forever in a version-controlled history that gets cloned, forked, and
pushed elsewhere*. Keeping samples on a filesystem path the repo's tooling
never touches removes that risk structurally instead of relying on
everyone remembering a rule.

Put samples somewhere like:

```
../sense_tool_eval_samples/          # sibling to sense_tool/
# or any absolute path outside the repo, e.g.
/home/you/private/hospital_samples/
```

`evaluate.py` refuses to run (hard error, not just a warning) if
`--samples-dir` resolves to anywhere inside the `sense_tool` repo. The
repo's `.gitignore` also has rules covering common sample-folder names as
defense-in-depth, in case someone puts data in the repo despite this policy
— but that's a backstop, not the actual control. Don't rely on it.

### Report output

Reports can contain extracted document text (i.e. patient data). By
default the report goes to **stdout only** — nothing is written to disk
unless you pass `--output`. If you do pass `--output`, the script refuses
any path inside the repo except `ocr_service_eval/reports/` (gitignored).
Prefer writing outside the repo entirely, same reasoning as above.

## Organizing samples (optional)

```
sense_tool_eval_samples/
  radiology/
    scan_001.pdf
    scan_002.png
  discharge_summary/
    doc_001.pdf
  ...
```

Subfolder name → category. If samples are flat (no subfolders), the script
tries to infer a category from the filename prefix (e.g.
`labs_report_003.pdf` → `labs_report`); if that fails too, everything lands
in one `uncategorized` group for you to sort out manually afterward — both
are explicitly noted in the report so you know which mode was used.

## What's measured, and an honest caveat

Per document: which path was taken (native-text shortcut vs
rasterize+OCR), processing time, average Tesseract word confidence (OCR
path only), extracted text length, whether Sense_tool's structure
heuristics found any sections/tables, and derived flags
(`low_confidence`, `near_empty_output`, `no_structure_detected`).

**"Path taken" and "confidence" are not literally reported by the `/ocr`
endpoint** — it only returns extracted text, and this script isn't allowed
to change that (see "Do not modify ocr_service" above). So both are
computed by this script itself:

- **Path taken** is inferred by independently checking whether the PDF has
  a usable text layer, using the exact same check
  (`pdfplumber` + a 20-char threshold) `ocr_service` uses internally.
- **Confidence** requires a *second*, local Tesseract pass
  (`pytesseract.image_to_data`, which the `/ocr` endpoint doesn't expose)
  using the same preprocessing `ocr_service` applies.

The **extracted text and processing time reported are always the real
values from the actual HTTP call** to the running service — only the two
diagnostics above involve a local recomputation, and the report's `meta`
section always says which mode (see below) produced them.

Two ways to run, trading off diagnostic accuracy vs setup:

### Option A (recommended): reuse the ocr_service Docker image

Runs this script inside a container built from the `sense_tool-ocr` image
(already has tesseract/poppler/opencv/pytesseract installed - nothing to
set up locally), with the whole repo mounted read-only so the script finds
`ocr_service/app/ocr.py` and `preprocess.py` next to itself and imports
them directly (read-only import, not a modification) — zero drift between
what the diagnostics measure and what the service actually does.

```bash
cd sense_tool
docker compose up -d ocr   # make sure the real service is running

docker run --rm \
  --network sense_tool_default \
  -v "$(realpath ../sense_tool_eval_samples)":/samples:ro \
  -v "$(pwd)":/sense_tool:ro \
  -w /sense_tool \
  sense_tool-ocr \
  sh -c "pip install -q httpx && python3 ocr_service_eval/evaluate.py \
    --samples-dir /samples --ocr-url http://ocr:8001"
```

### Option B: plain local venv

Needs `tesseract-ocr` and `poppler-utils` installed locally (same as
`ocr_service` needs them). Diagnostics use this script's own
reimplementation of the preprocessing/config logic (kept in sync by hand
with `ocr_service/app/`, noted in code comments).

```bash
cd sense_tool/ocr_service_eval
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

docker compose -f ../docker-compose.yml up -d ocr  # or point --ocr-url elsewhere

python3 evaluate.py \
  --samples-dir ../../sense_tool_eval_samples \
  --ocr-url http://localhost:8001
```

## CLI options

```
--samples-dir PATH        required (or OCR_EVAL_SAMPLES_DIR env var)
--ocr-url URL             default http://localhost:8001 (or OCR_SERVICE_URL)
--psm N                   Tesseract page segmentation mode, default 6
--low-confidence-threshold N   percent, default 60
--min-text-chars N        near-empty-output threshold, default 20
--format markdown|json    default markdown
--output PATH             write report to PATH instead of stdout (see policy above)
```

## Report contents

- **Aggregate table**: overall + per-category counts, error count, native
  vs OCR path split, avg processing time, avg confidence, % low-confidence,
  % no-structure-detected, % near-empty-output.
- **Per-document table**, grouped by category: filename, path taken, time,
  confidence, text length, sections/tables detected, flags.

`no_structure_detected` and `near_empty_output` are heuristic flags, not
certain failures — there's no ground truth here about what a document
*should* contain, so treat them as "worth a human look," not "definitely
broken."
