"""WP-G: build a Tesseract "searchable PDF" for scanned documents.

A searchable PDF is the *original* page image with an invisible, exact-
word-position OCR text layer over it: the visual is a byte-faithful copy
of the upload (it literally is the original raster), and the text becomes
selectable / searchable / copyable.

This is produced by a **separate, asynchronous** job in the main app's Arq
chain (`app.worker.run_searchable_pdf_generation`), never inline with text
extraction. It is deliberately decoupled from `Document.extracted_text`:

  * The primary extraction path (OpenCV Otsu/deskew + `--oem 1 --psm 6`)
    stays exactly as validated - see `app/ocr.py`. An A/B evaluation on 7
    real scanned documents showed the un-preprocessed path measurably
    regressing `extracted_text` on real scans (column loss on a lab
    report, table-structure loss on a DEXA printout, near-total collapse
    on a skewed photo), so the two are now separate passes.

  * The searchable PDF still has to render the ORIGINAL image as its
    visible layer (Tesseract always draws whatever it OCRs as the PDF
    background), so this pass runs on the un-preprocessed image. But it
    pins `--oem 1 --psm 6` like the primary path - `run_tesseract` is
    called directly (not `run_and_get_multiple_output`, which accepts no
    config) so the invisible text layer keeps the same page-segmentation
    behaviour that matters for tabular scans.

  * `lang` is threaded straight through, so `eng+guj` / `eng+nep` produce
    a correct Unicode invisible layer in the matching script.

A single Tesseract invocation per page emits both `txt` and `pdf`; the
`txt` is discarded here (the primary path owns `extracted_text`).
"""

import io
import logging
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pytesseract.pytesseract import run_tesseract, save

logger = logging.getLogger("ocr_service.searchable_pdf")

# Mirrors app/ocr.py's pinned Tesseract config for the primary path.
_PSM = 6
_OEM = 1
_TESS_CONFIG = f"--oem {_OEM} --psm {_PSM}"

# Per-page Tesseract wall-clock ceiling. The A/B eval saw ~21 s for the
# worst single page (a 5100x6600 photographed printout); 180 s is a very
# generous cap that still stops a pathological page from wedging the job.
_PER_PAGE_TIMEOUT_S = 180


class SearchablePdfError(RuntimeError):
    """Raised when a searchable PDF cannot be produced."""


def _ocr_page_to_pdf(page_image, lang: str) -> bytes:
    """One `tesseract <in> <out> -l <lang> --oem 1 --psm 6 txt pdf` call on
    `page_image` (a PIL image - the ORIGINAL page, not preprocessed).

    Returns the one-page PDF bytes; the visible layer is `page_image`
    itself, the text layer is the invisible OCR overlay.
    """
    with save(page_image) as (tmp_base, input_file):
        try:
            run_tesseract(
                input_filename=input_file,
                output_filename_base=tmp_base,
                extension="txt pdf",
                lang=lang,
                config=_TESS_CONFIG,
                timeout=_PER_PAGE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - normalise to a domain error
            raise SearchablePdfError(f"tesseract failed on a page: {exc}") from exc
        pdf_path = Path(f"{tmp_base}.pdf")
        if not pdf_path.exists():
            raise SearchablePdfError("tesseract produced no PDF for a page")
        data = pdf_path.read_bytes()
    if not data:
        raise SearchablePdfError("tesseract produced an empty PDF for a page")
    return data


def merge_page_pdfs(page_pdfs: list[bytes]) -> bytes:
    """Concatenate per-page single-page PDFs into one multi-page PDF, in the
    order given (document page order). A single page is still passed through
    the writer so the output is always uniformly structured."""
    if not page_pdfs:
        raise SearchablePdfError("no page PDFs to merge")
    writer = PdfWriter()
    for i, pdf_bytes in enumerate(page_pdfs):
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
        except Exception as exc:  # noqa: BLE001
            raise SearchablePdfError(f"unreadable page PDF at index {i}: {exc}") from exc
        for page in reader.pages:
            writer.add_page(page)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def build_searchable_pdf(filename: str, data: bytes, lang: str) -> bytes:
    """Produce a searchable PDF for an uploaded image or scanned PDF.

    - image upload  -> a single OCR'd page
    - PDF upload    -> every page rasterised (poppler) then OCR'd, merged
                       in page order

    Always rasterise+overlay: this endpoint's job is "make a searchable
    PDF", and the native-PDF pass-through case is handled upstream (the
    export router streams the original file for `text_source=native_pdf`).
    """
    if not data:
        raise SearchablePdfError("empty file")

    from PIL import Image

    is_pdf = Path(filename).suffix.lower() == ".pdf" or data[:4] == b"%PDF"

    if is_pdf:
        from pdf2image import convert_from_bytes

        pages = [p.convert("RGB") for p in convert_from_bytes(data)]
    else:
        pages = [Image.open(io.BytesIO(data)).convert("RGB")]

    if not pages:
        raise SearchablePdfError("no pages to OCR")

    page_pdfs = [_ocr_page_to_pdf(pg, lang) for pg in pages]
    return merge_page_pdfs(page_pdfs)
