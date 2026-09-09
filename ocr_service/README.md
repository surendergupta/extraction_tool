# ocr_service

Standalone OCR microservice for Sense_tool. See [../README.md](../README.md)
("OCR service" section) for full documentation — architecture, the `/ocr`
endpoint, how it's called, running it, and testing it. This file covers
only what's specific to this directory.

## OCR language (`lang` param) — multi-script support

The Tesseract rasterize+OCR path (images, and PDFs with no text layer) can
recognise **Gujarati** and **Nepali/Devanagari** alongside English in a
single pass, via a combined Tesseract language string.

### `POST /ocr` — `lang` query param

| Value | Effect |
|---|---|
| *(omitted)* | `eng` — **unchanged** English-only behaviour. Byte-for-byte the same command line (`--oem 1 --psm <psm> -l eng`) as before this param existed. |
| `eng` | Same as omitting it. |
| `eng+guj` | English + Gujarati in one pass. |
| `eng+nep` | English + Nepali (Devanagari script). |
| `guj`, `nep`, `eng+guj+nep`, … | Any `+`-joined combination of installed codes. |

Rules:

- Only affects the **Tesseract path**. Native-PDF-text extraction returns
  the PDF's own Unicode text layer, which is already script-correct
  regardless of `lang`.
- Shape is validated (`^[a-z]{3}(\+[a-z]{3})*$`) — a malformed value is a
  `422`.
- Every sub-code must have tessdata **baked into the image** (see
  Dockerfile: `tesseract-ocr-{eng,guj,nep}`). A code that isn't installed
  is a `400` with a clear message — *not* a silent downgrade. (Tesseract
  itself, given `eng+zzz` with no `zzz.traineddata`, silently drops `zzz`
  and runs as `eng`; the endpoint refuses that ambiguity explicitly.)
- Nothing is fetched at runtime. Verify what's actually in the image:
  `docker run --rm sense_tool-ocr tesseract --list-langs`.

The HTTP client (`app/services/ocr.py::extract_text`) takes an optional
`lang` argument and only sends the param when a caller passes one;
otherwise the request is identical to before.

### Accuracy tradeoff — why `lang` is opt-in (real-photo validation)

**Combined-language mode severely degrades English-text recognition on real
photographed documents. This is measured, not a theoretical risk.**
Validated against two actual mixed-script medical documents — a
Gujarati-letterhead Gulf-employment medical report and a Nepali
foreign-employment medical report — plus a real 4-page English
native-text lab report for the native-path check.

**Native-PDF-text path is unaffected.** On the real 4-page English PDF, all
of `eng`, `eng+guj`, `eng+nep`, `eng+guj+nep` and `lang` omitted returned
**byte-identical** text. `lang` only touches the Tesseract rasterize+OCR
path; a PDF with a text layer never reaches it.

**On real scans, combined mode corrupts most of the English body text.**
Similarity is measured against the `-l eng` output on the same region:

- Gujarati document, cropped to its **pure-English lab-results tables**
  (zero Gujarati content in that region): `eng+guj` changed ~49% of the
  OCR text vs `eng` (similarity 0.51) and injected **327 hallucinated
  Gujarati characters**; `eng+guj+nep` left only ~11% of the text intact
  (similarity 0.11).
- Nepali document (body is ~100% English by design — Devanagari only
  inside small stamps): `eng+nep` similarity 0.51 with **132–145
  hallucinated Devanagari characters** scattered across the English form;
  `eng+guj` — a script the document does not contain at all — similarity
  0.59 with **187–209 hallucinated Gujarati characters**.
- Across regions, roughly **50–90% of the English body text was
  changed or corrupted** by combined mode. Concrete field-level damage:
  passport-number line, nationality field, date fields, lab-unit tokens
  and table headers were partly overwritten with native-script characters.

**The mechanism is hallucination, not just misreading text.** The added
language model reads non-text scan artefacts — form rules, stamps,
signatures, a passport photo, blue-ink bleed, general scan noise — as
native script. This is why a document that is ~100% English (native script
confined to small stamps) still accumulated 130+ spurious Devanagari
characters: there was almost no real Devanagari to find, so the model
painted it onto noise. A clean synthetic render has none of these
artefacts, which is why earlier synthetic-only testing showed a near-zero
English cost and character-exact script — that result **understated the
real risk** and should not be relied on.

**Tesseract's word-confidence score is not a usable guard here — it moves
the wrong way.** In every region of both documents, mean per-word
confidence *rose* in combined mode (+4 to +20 percentage points) while
similarity to the English baseline *collapsed*. Gujarati English body:
confidence 32.0% → 46.9% (`eng+guj`) → 50.0% (`eng+guj+nep`) as similarity
fell 1.00 → 0.51 → 0.11. The model reports high confidence for noise it can
now "resolve" to a native-script glyph. Confidence-based gating — including
the eval harness's `low_confidence` flag (threshold 60%) — would fire
*less* on the worse output, not more.

**Cross-script behaviour.** The two script blocks do not bleed into each
other: an `eng+guj` run produced ≤1 Devanagari character, an `eng+nep` run
produced 0 Gujarati. But each added language hallucinates *its own* script
onto noise whether or not the document contains it (see the `eng+guj` run
on the Nepali document above). `eng+guj+nep` emits both scripts mixed and
was the worst option on every metric in every region.

**Where combined mode did show real signal:** only when applied to an
*isolated* native-script region rather than a whole mixed-script page.
Cropped to just the Gujarati letterhead band (top ~16% of the page),
`eng+guj` produced 82 Gujarati characters where `eng` produced Latin
gibberish (region confidence 60% → 77%, similarity 0.67; correctness not
verifiable without ground truth). Full-page application is **not**
recommended even when native-script content genuinely exists on the page —
the gain is confined to the script region and the cost is spread across
the whole English body.

**Decision: `lang` is strictly opt-in per request, default `eng`, now
backed by real-photo evidence** (previously only by the more cautious
synthetic-test reasoning). The `lang`-omitted path is the old code path
exactly, so existing English-only callers and the eval harness are
unaffected. A "smart default" keyed on document source is *not* viable:
even when a document is known to have a Gujarati letterhead, enabling
`eng+guj` for the whole page wrecks the English lab tables that carry the
clinical payload. If native-script letterhead/prose is specifically
needed, run it as a *second* pass over a cropped native-script region,
alongside — not instead of — the `eng` pass.

## Searchable PDF (`POST /searchable-pdf`, WP-G)

`POST /searchable-pdf` (multipart `file`, optional `lang`) returns
`application/pdf`: the **original page image(s)** with an invisible,
word-positioned OCR text layer — a PDF whose visual *is* the scan and whose
text is now selectable/searchable. It is what Sense_tool's `format=pdf`
export streams for scanned documents (native-PDF documents export their own
file untouched — that's the export router's job, not this endpoint's).

- **A standalone endpoint on its own Tesseract pass — on purpose.** The
  main app calls it from a *follow-up* Arq job (`run_searchable_pdf_generation`)
  **after** text extraction, so a failure here can never touch
  `Document.extracted_text`, `status`, or the pipeline outcome. `/ocr` has
  no searchable-PDF coupling at all.
- **Why a second pass, not one.** An A/B evaluation on 7 real scanned
  documents (DEXA printout photo, Gulf Gujarati/Nepali forms, workplace lab
  report, a heavily skewed photo, two digital DEXA renders) compared the
  tuned primary `/ocr` pass against a single un-preprocessed pass feeding
  `extracted_text`. The un-preprocessed one was **measurably worse on real
  scans** — a lab report lost its units/reference-range columns, a DEXA
  printout lost its results-table row↔value association, a skewed photo
  collapsed to two tokens — while reporting *higher* confidence (fewer,
  easier words). So the two are separate: `/ocr` keeps its OpenCV
  Otsu/deskew + `--oem 1 --psm 6` pipeline, byte-for-byte unchanged.
- **This pass:** `run_tesseract` (directly — `run_and_get_multiple_output`
  takes no config) with `--oem 1 --psm 6` in one `tesseract … txt pdf`
  invocation per page, on the **un-preprocessed** original image. Tesseract
  always renders whatever it OCRs as the visible PDF layer, so it must see
  the original; pinning psm keeps the invisible layer's segmentation sane
  for tabular scans. The deskew/binarise difference from the primary path
  is the one unavoidable gap and affects only the invisible layer.
- **Multi-page** scanned PDFs are re-rasterised (poppler) and the per-page
  PDFs merged in page order with `pypdf`.
- **`lang` is honoured.** `eng`, `eng+guj`, `eng+nep`, … produce an
  invisible text layer in the matching script, written as proper Unicode
  (selectable and searchable), verified in the test suite. Pass the same
  value the document was OCR'd with (`Document.ocr_lang`).

## Image / photo / chart region extraction (`images`)

`/ocr` responses carry an `images` array: embedded raster images (native-PDF
path) and detected photo/chart/logo/stamp regions (rasterise+OCR path),
each cropped and returned so a later work package can preserve them
visually. **Scope here is detection + extraction only** — nothing is wired
into DOCX/PDF/XLSX export, and text/table extraction is completely
unchanged (additive). Toggle with `?extract_images=false`.

Each entry:

| field | meaning |
|---|---|
| `bbox` | `[x0, y0, x1, y1]` in `bbox_space` units |
| `bbox_space` | `"pdf_points"` (embedded, page-relative, top-left origin) or `"page_pixels"` (detected, pixels of the rasterised page) |
| `page` | 0-based page index |
| `source` | `"embedded"` (pdfplumber `page.images`) or `"detected"` (heuristic detector) |
| `region_type_guess` | `photo` \| `chart` \| `logo` \| `stamp` \| `unknown` — **a low-confidence guess, not a claim of accuracy** |
| `width`,`height`,`format` | of the returned crop (`jpg`/`png`/`jp2`) |
| `image_base64` | the crop bytes |

### Native-PDF path — reliable

`pdfplumber` already exposes every embedded raster with a real bbox. We
hand back the original stream bytes for the common photographic filters
(`DCTDecode`/JPEG, `JPXDecode`), else re-render the page region at 200 DPI.
Identical images repeated on every page (letterhead logo, QR, signature)
are de-duplicated by content hash. Verified against **LabReport-1.pdf**:
its 5 distinct embedded images — the letterhead banner (with an embedded
portrait), a sub-logo, a footer banner, a QR code, and a
signature/credential stamp — all come out as valid, visually-correct JPEG
bytes with correct boxes.

### Scanned / rasterised path — a heuristic, honestly partial detector

There is no embedded-image list for a scanned page, so sub-regions have to
be *detected*. This is a genuinely hard CV problem; the detector
(`app/images.py`) is a tuned heuristic, not a solved one. What actually
separated pictures from text/tables on the real samples it was built
against (scanned & digital DEXA reports, Gulf-employment medical forms, a
text-only lab report):

- a **picture** is one large connected mass of non-text content — strongly
  coloured (`hi_sat_frac`), a dark continuous-tone scan (`black_frac`), or
  a pale continuous-tone scan (much *soft* darkening vs a local-median
  background, little *hard* text-stroke darkening);
- a **text block** is thousands of tiny disconnected marks — high glyph
  coverage, no large solid blob;
- a **ruled table** is long thin lines — high `ruled` score, low blob, low
  colour.

Local-background subtraction removes page tint, zebra row-striping and pale
table fills before any of this runs.

**Measured behaviour (real samples, judged against a visual ground truth):**

| sample (type) | true regions | detected | false positives | missed |
|---|---|---|---|---|
| scanned DEXA report (photo of a printout) | 2 bone scans, 2 colour charts, 2 tiny vendor logos | 2 charts ✓, 2 scans ✓ (one scan over-split into 2 crops) | **0** | 2 tiny logos |
| digital DEXA report A | 2 line charts, 6 body scans, 2 logos | 2 charts (partial box), 6 scans as **1** strip, 1 logo | 0 | 2 thin logos; scans not individuated |
| digital DEXA report B | 2 body scans, 1 BMI colour bar | 2 scans (over-split into ~4) | 0 | BMI bar |
| workplace lab report (mostly monospace text) | 1 logo, 1 QR | logo ✓, QR ✓ | **0** | 0 |
| Gujarati employment form (faded scan) | 1 passport photo, 1 round stamp, 1 caduceus logo | caduceus logo ✓, stamp (partial) | 0 | **passport photo** (too faded — barely darker than the washed-out page) |
| Nepali employment form | 1 passport photo, 2 stamps | passport photo ✓ | 0 | 2 stamps (ruled borders trip the table filter) |
| text-only page (negative control) | 0 | 0 | **0** | — |

**Honest limitations:**

- **Recall is partial.** Strong on large chart / scan blocks; weak on small
  logos, faded photos, and stamps whose ruled border makes them look like a
  table cell.
- **Over-segmentation.** A scan crossed by vector overlay lines (DEXA ROI
  boxes, measurement rules) can come back as 2–4 adjacent crops instead of
  one. Crop boxes on charts are sometimes tight to the plot area and clip
  the axis labels/title.
- **`region_type_guess` is weak** — e.g. logos are often labelled `chart`,
  stamps `photo`. Treat it as a hint only.
- **QR codes** decode as `unknown` (they are a grid of small marks — genuinely
  ambiguous against text).
- **No false positives on body text or tables** in any sample tested,
  including the negative control. This is the property deliberately
  favoured: a text block wrongly returned as an "image" would be dropped by
  any downstream text consumer, which is worse than missing a picture.
- Caps: at most 12 regions/response; crops downscaled to a 1600 px long
  edge; photo/chart crops re-encoded JPEG q85, others PNG.

## Table extraction — two paths, two shapes (`tables` vs `table_regions`)

The `/ocr` response carries table output in **two fields with different
shapes**, and `text_source` says which path ran:

| `text_source` | field | shape | how |
|---|---|---|---|
| `"native_pdf"` | `tables` | `list[list[list[str \| None]]]` — a real grid per table | pdfplumber `extract_tables()` on the PDF's own page objects (unchanged) |
| `"ocr"` | `table_regions` | `[{bbox, page, region_text, source:"ruled_line_region"}]` — a **text blob, NO grid** | WP-D ruled-line detector (`app/tables.py`) |

`tables` is always `[]` on the OCR path and `table_regions` always `[]` on
the native path. **A consumer must branch on the entry's `source` and
must not assume a uniform shape** — this is deliberate (WP-D), not an
oversight to be unified later.

### Ruled-line table detection (rasterize+OCR path only)

WP-C and WP-D measured the old space-alignment table heuristic as **pure
garbage on scanned pages** (1–12 bogus "tables" per page, real tables never
reconstructed) and found PaddleOCR's PP-Structure pipeline **crashes on
this CPU stack**. WP-D's evaluation of classical CV recommended a
*reduced* approach, which is what shipped here:

1. Morphology (long thin H/V structuring elements) finds ruled-table
   **bounding boxes**; grid lines are then painted white and the box is
   **OCR'd as one block** (`--psm 6`). There is **no** row/column
   reconstruction and **no** cell-by-cell OCR — WP-D showed both are a net
   loss.
2. A **false-positive filter** (`_looks_like_data_table`) rejects:
   - strongly coloured / multi-hue regions (colourfulness > 60 and
     saturation > 75) — DEXA reference-chart borders;
   - regions with almost no paper-white (< 20 %) — photo / scan strips;
   - small wide boxed grids (≥ 5 columns, ≤ 12 rows, roughly square) —
     the Gulf-employment CANDIDATE INFORMATION form-field block.

**Measured before → after** (WP-D sample set; "OLD" = space-heuristic):

| sample | OLD | NEW (ruled-line) |
|---|---|---|
| DEXA scanned photo | 6 one-row junk "tables" | **1 region = the genuine Region/BMD/T-Score table**, `region_text` a near-complete clean read; chart borders + patient band suppressed |
| DEXA digital report | 1 junk table | **5 regions = all 4 genuine data tables** (full clean reads) + patient band; 6-panel scan strip suppressed |
| Gulf Gujarati form | 6 junk tables | **1 region = the Medical-Exam + Lab-Investigation table** (real values: `120/80`, `ABSENT`, `NAD`); CANDIDATE INFORMATION grid suppressed |
| Gulf Nepali form | 12 junk tables | **2 regions = the two exam tables** |
| Workplace lab report | 0 | **0** — borderless (see limitation below) |
| Negative control (text only) | 0 | **0** |
| Faded / skewed Gulf scans | 2–9 junk | main table region still found (deskew handled +4°); 1 residual small FP on the heavily-faded variant |

Latency: 0.1–4 s/page on a weak 2-core CPU (well inside the 240 s sync
budget). The native-PDF path is **completely untouched** — same
`extract_tables()` grids, verified byte-identical on LabReport-1.pdf.

**FP-filter tradeoff (honest):** the form-grid rule keys on grid *shape*
only (no cell-content analysis), so a genuinely small, wide, fully-ruled
data table (≤ 12 rows **and** ≥ 5 columns) would also be suppressed — none
exists in the sample set; tall tables and ≤ 4-column tables are unaffected.
The colour rule is tuned to pass the pale single-hue tint of a coloured
results table (colourfulness ~45) and could still suppress a table printed
with a vivid saturated fill.

## Known limitations

- **Borderless / whitespace-aligned tables are not detected** by the
  ruled-line path — no rules to find. Confirmed in WP-D on the workplace
  lab report, whose entire body is one monospace column table: it returns
  **0** `table_regions`. This is the common lab-report layout and remains
  an open gap (a whitespace/column-alignment detector would be a separate
  work package). The old space-heuristic is *not* kept as a fallback here
  because WP-C/WP-D proved it emits pure garbage on OCR text; the
  space-aligned lines are preserved verbatim in `text` / the parsed
  section content instead.

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

- **The rasterize+OCR path defaults to English; non-English scripts are
  opt-in and come with a heavy cost.** With `lang` omitted,
  `_extract_image`/`_extract_pdf_via_ocr` run Tesseract `-l eng` only (see
  `app/ocr.py::_tesseract_config`) — a Hindi/Devanagari, Gujarati or other
  non-English document at that default produces Latin garbage or nothing.
  `lang=eng+guj` / `eng+nep` will engage those scripts, but the real-photo
  validation above shows full-page combined mode badly corrupts the
  English text on the same page and hallucinates native script onto scan
  noise — so it is not a general fix for mixed-script scans. Other scripts
  (e.g. Hindi) still need their own language pack added.

  A cleaner workaround for the image-baked-script case is deferred, not
  built: OCR the image regions specifically (via `page.images` bounding
  boxes) with the right language pack — a cropped native-script region is
  the one place combined mode showed real signal. This needs image-region
  classification first — a page's images aren't all "banner text" (one
  real letterhead also has two unrelated portrait photos), so OCR-ing
  every image blindly would produce garbage on the non-text ones.
