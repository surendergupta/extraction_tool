# ocr_service

Standalone OCR microservice for Sense_tool. See [../README.md](../README.md)
("OCR service" section) for full documentation — architecture, the `/ocr`
endpoint, how it's called, running it, and testing it. This file covers
only what's specific to this directory.

## Known limitations

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
  above: `_extract_image`/`_extract_pdf_via_ocr` run Tesseract with
  `-l eng` only (see `app/ocr.py::_tesseract_config`). Even a document
  that *did* go through OCR instead of native extraction would not have
  its Hindi/Devanagari (or any other non-English) text correctly
  recognized — Tesseract would read it with the English model and produce
  garbage or nothing, not real text. This is a different limitation from
  the one above (OCR accuracy vs. images having no text at all) and would
  need its own fix (a non-English language pack) if it mattered.

  A workaround for the image-content case is deferred, not built: OCR the
  image regions specifically (via `page.images` bounding boxes) with a
  Hindi language pack. This needs image-region classification first — a
  page's images aren't all "banner text" (that same letterhead also has
  two unrelated portrait photos), so OCR-ing every image blindly would
  produce garbage on the non-text ones.
