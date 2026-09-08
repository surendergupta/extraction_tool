#!/usr/bin/env python3
"""Standalone OCR accuracy-evaluation harness for ocr_service.

NOT a pytest suite - this is a manual tool a developer runs by hand against
real hospital sample documents to measure OCR behavior/accuracy. It talks to
the real, already-running ocr_service ONLY over its public HTTP API
(POST /ocr), exactly like any production client would - this script never
modifies ocr_service's code or behavior, and never imports it in-process.

Sample documents (real patient data) must live OUTSIDE this repo. See
README.md in this directory for the full policy, and DO NOT point
--samples-dir at anything under the sense_tool repo (the script refuses to
run if you do, as defense-in-depth on top of that policy).

Usage:
    python3 evaluate.py --samples-dir /path/to/samples [options]

    # or via env vars
    OCR_EVAL_SAMPLES_DIR=/path/to/samples python3 evaluate.py

See README.md for the two ways to run this (plain local venv, or via the
ocr_service Docker image for zero-drift diagnostics).
"""

import argparse
import importlib
import io
import json
import os
import re
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import cv2
import httpx
import numpy as np

_EVAL_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _EVAL_DIR.parent  # sense_tool/
_OCR_SERVICE_DIR = _REPO_ROOT / "ocr_service"  # sense_tool/ocr_service/


@contextmanager
def _isolated_import_root(root: Path):
    """Temporarily put `root` first on sys.path, then undo it - including
    unregistering any modules imported during the block from sys.modules.

    Needed because ocr_service/app/ and sense_tool/app/ are two different
    packages that both happen to be named `app`: importing one normally
    would get cached in sys.modules under the name "app" and silently
    shadow the other for the rest of the process. Grab the specific
    functions/constants you need out of the `yield`ed block; don't hold
    onto the module object itself past it.
    """
    root_str = str(root)
    sys.path.insert(0, root_str)
    pre_existing = set(sys.modules)
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name not in pre_existing:
                del sys.modules[name]
        try:
            sys.path.remove(root_str)
        except ValueError:
            pass


# --- Reuse ocr_service's own preprocessing/config, read-only, when available ---
#
# The /ocr HTTP endpoint only returns extracted text - it doesn't report
# which path it took (native-text vs rasterize+OCR) or Tesseract's
# per-word confidence, and this script is not allowed to modify
# ocr_service to expose that. So those two diagnostics are computed here,
# independently, via a *second*, local Tesseract pass - never used as the
# reported "extracted text" (that always comes from the real HTTP call).
#
# When ocr_service/ is present next to this script (true whenever the full
# sense_tool repo is available - both the plain local-venv way and the
# recommended docker-run way in README.md mount the whole repo), this
# imports the actual shipped functions - zero duplication, zero drift from
# production. Otherwise it falls back to a local reimplementation of the
# same logic (kept in sync by hand - see the comments below).
REUSING_SERVICE_MODULES = False
if (_OCR_SERVICE_DIR / "app" / "ocr.py").exists():
    try:
        with _isolated_import_root(_OCR_SERVICE_DIR):
            _ocr_mod = importlib.import_module("app.ocr")
            _preprocess_mod = importlib.import_module("app.preprocess")
            NATIVE_TEXT_MIN_CHARS = _ocr_mod._NATIVE_TEXT_MIN_CHARS
            try_native_pdf_text = _ocr_mod._try_native_pdf_text
            tesseract_config = _ocr_mod._tesseract_config
            preprocess_image = _preprocess_mod.preprocess_image
        REUSING_SERVICE_MODULES = True
    except Exception:
        REUSING_SERVICE_MODULES = False

if not REUSING_SERVICE_MODULES:
    # Mirrors ocr_service/app/ocr.py::_NATIVE_TEXT_MIN_CHARS - keep in sync.
    NATIVE_TEXT_MIN_CHARS = 20

    def tesseract_config(psm: int) -> str:
        # Mirrors ocr_service/app/ocr.py::_tesseract_config.
        return f"--oem 1 --psm {psm} -l eng"

    def preprocess_image(img_bgr: np.ndarray) -> np.ndarray:
        # Mirrors ocr_service/app/preprocess.py::preprocess_image.
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        _, binarized = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        return _deskew(binarized)

    def _deskew(img: np.ndarray, max_correction_deg: float = 15.0) -> np.ndarray:
        # Mirrors ocr_service/app/preprocess.py::_deskew.
        coords = np.column_stack(np.where(img < 255))
        if coords.shape[0] < 50:
            return img
        angle = cv2.minAreaRect(coords)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if abs(angle) < 0.1 or abs(angle) > max_correction_deg:
            return img
        h, w = img.shape[:2]
        matrix = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
        return cv2.warpAffine(
            img, matrix, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
        )

    def try_native_pdf_text(data: bytes) -> str | None:
        # Mirrors ocr_service/app/ocr.py::_try_native_pdf_text.
        import pdfplumber

        try:
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                page_texts = [(p.extract_text() or "").strip() for p in pdf.pages]
        except Exception:
            return None
        combined = "\n\n".join(t for t in page_texts if t)
        return combined if len(combined) >= NATIVE_TEXT_MIN_CHARS else None


# Sense_tool's structure-parsing heuristics - imported directly (pure
# stdlib, no heavy deps) so "sections/tables detected" reflects exactly
# what the real pipeline would produce for this text. Same isolated-import
# trick as above, and for the same reason (sense_tool/app/ vs
# ocr_service/app/ are different packages that share the name "app").
with _isolated_import_root(_REPO_ROOT):
    parse_structure = importlib.import_module("app.services.structure").parse_structure

SUPPORTED_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}


@dataclass
class DocResult:
    filename: str
    relative_path: str
    category: str
    path_taken: str  # "native" | "ocr" | "error"
    response_time_seconds: float | None
    avg_confidence: float | None
    text_length: int
    sections_detected: int
    tables_detected: int
    flags: list[str] = field(default_factory=list)
    error: str | None = None


def discover_samples(samples_dir: Path) -> list[Path]:
    return sorted(
        p for p in samples_dir.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
    )


def infer_category(file_path: Path, samples_root: Path) -> str:
    rel = file_path.relative_to(samples_root)
    if len(rel.parts) > 1:
        return rel.parts[0]
    # Flat layout: try a filename-based prefix (letters/underscore/dash
    # before the first digit, e.g. "radiology_report_003.pdf" -> "radiology_report").
    m = re.match(r"^([A-Za-z_-]{3,})", file_path.stem)
    return m.group(1).strip("_-").lower() if m else "uncategorized"


def measure_confidence(data: bytes, is_pdf: bool, psm: int) -> float | None:
    """A second, local Tesseract pass purely to measure word confidence via
    pytesseract.image_to_data - the production /ocr response doesn't expose
    this. Uses the same preprocessing as ocr_service (see module docstring)."""
    import pytesseract
    from PIL import Image

    if is_pdf:
        from pdf2image import convert_from_bytes

        pil_pages = convert_from_bytes(data)
    else:
        pil_pages = [Image.open(io.BytesIO(data)).convert("RGB")]

    confidences: list[float] = []
    for page in pil_pages:
        cv_img = cv2.cvtColor(np.array(page.convert("RGB")), cv2.COLOR_RGB2BGR)
        processed = preprocess_image(cv_img)
        tsv = pytesseract.image_to_data(
            processed, config=tesseract_config(psm), output_type=pytesseract.Output.DICT
        )
        for level, conf, text in zip(tsv.get("level", []), tsv.get("conf", []), tsv.get("text", [])):
            if int(level) != 5 or not text.strip():  # level 5 = word
                continue
            try:
                c = float(conf)
            except (TypeError, ValueError):
                continue
            if c >= 0:
                confidences.append(c)

    return (sum(confidences) / len(confidences)) if confidences else None


def evaluate_document(
    file_path: Path,
    samples_dir: Path,
    ocr_url: str,
    psm: int,
    low_confidence_threshold: float,
    min_text_chars: int,
) -> DocResult:
    category = infer_category(file_path, samples_dir)
    relative_path = str(file_path.relative_to(samples_dir))
    data = file_path.read_bytes()
    is_pdf = file_path.suffix.lower() == ".pdf" or data[:4] == b"%PDF"

    # 1. The real, running service - production text + timing, via HTTP
    #    only, exactly like any other client of this API.
    try:
        t0 = time.monotonic()
        response = httpx.post(
            f"{ocr_url}/ocr",
            params={"psm": psm},
            files={"file": (file_path.name, data)},
            timeout=httpx.Timeout(connect=5.0, read=240.0, write=10.0, pool=5.0),
        )
        elapsed = time.monotonic() - t0
        response.raise_for_status()
        text = response.json()["text"]
    except Exception as exc:  # noqa: BLE001 - report as a failed row, keep going
        return DocResult(
            filename=file_path.name,
            relative_path=relative_path,
            category=category,
            path_taken="error",
            response_time_seconds=None,
            avg_confidence=None,
            text_length=0,
            sections_detected=0,
            tables_detected=0,
            flags=["request_failed"],
            error=str(exc),
        )

    flags: list[str] = []

    # 2. Path taken - inferred (the endpoint doesn't report this): a PDF
    #    took the native-text shortcut iff it has a usable text layer.
    path_taken = "ocr"
    if is_pdf:
        path_taken = "native" if try_native_pdf_text(data) is not None else "ocr"

    # 3. Confidence - only meaningful for documents that actually ran OCR.
    avg_confidence = None
    if path_taken == "ocr":
        try:
            avg_confidence = measure_confidence(data, is_pdf, psm)
        except Exception as exc:  # noqa: BLE001
            flags.append(f"confidence_measurement_failed:{exc}")
        if avg_confidence is not None and avg_confidence < low_confidence_threshold:
            flags.append("low_confidence")

    # 4. Extracted-text sanity check.
    text_length = len(text)
    if len(text.strip()) < min_text_chars:
        flags.append("near_empty_output")

    # 5. Structure-detection sanity check (Sense_tool's real heuristics).
    structured = parse_structure(text)
    sections_detected = len(structured["sections"])
    tables_detected = len(structured["tables"])
    if sections_detected == 0 and tables_detected == 0:
        flags.append("no_structure_detected")

    return DocResult(
        filename=file_path.name,
        relative_path=relative_path,
        category=category,
        path_taken=path_taken,
        response_time_seconds=elapsed,
        avg_confidence=avg_confidence,
        text_length=text_length,
        sections_detected=sections_detected,
        tables_detected=tables_detected,
        flags=flags,
        error=None,
    )


def _stats_for(results: list[DocResult]) -> dict:
    n = len(results)
    errors = [r for r in results if r.error]
    timed = [r.response_time_seconds for r in results if r.response_time_seconds is not None]
    conf = [r.avg_confidence for r in results if r.avg_confidence is not None]
    return {
        "count": n,
        "errors": len(errors),
        "native_path_count": sum(1 for r in results if r.path_taken == "native"),
        "ocr_path_count": sum(1 for r in results if r.path_taken == "ocr"),
        "avg_processing_time_seconds": (sum(timed) / len(timed)) if timed else None,
        "avg_confidence": (sum(conf) / len(conf)) if conf else None,
        "pct_low_confidence": (sum(1 for r in results if "low_confidence" in r.flags) / n * 100) if n else None,
        "pct_no_structure_detected": (
            sum(1 for r in results if "no_structure_detected" in r.flags) / n * 100
        )
        if n
        else None,
        "pct_near_empty_output": (
            sum(1 for r in results if "near_empty_output" in r.flags) / n * 100
        )
        if n
        else None,
    }


def aggregate(results: list[DocResult]) -> dict:
    by_category: dict[str, list[DocResult]] = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)
    return {
        "overall": _stats_for(results),
        "by_category": {cat: _stats_for(rs) for cat, rs in sorted(by_category.items())},
    }


def render_json(results: list[DocResult], agg: dict, meta: dict) -> str:
    return json.dumps(
        {"meta": meta, **agg, "documents": [asdict(r) for r in results]}, indent=2
    )


def _fmt(v, suffix: str = "", digits: int = 1) -> str:
    return "n/a" if v is None else f"{v:.{digits}f}{suffix}"


def _stats_table_row(label: str, s: dict) -> str:
    return (
        f"| {label} | {s['count']} | {s['errors']} | {s['native_path_count']} | {s['ocr_path_count']} "
        f"| {_fmt(s['avg_processing_time_seconds'], 's', 2)} | {_fmt(s['avg_confidence'], '%')} "
        f"| {_fmt(s['pct_low_confidence'], '%')} | {_fmt(s['pct_no_structure_detected'], '%')} "
        f"| {_fmt(s['pct_near_empty_output'], '%')} |"
    )


def render_markdown(results: list[DocResult], agg: dict, meta: dict) -> str:
    lines = ["# OCR service evaluation report", ""]
    lines.append(f"- Generated: {meta['generated_at']}")
    lines.append(f"- Samples dir: `{meta['samples_dir']}`")
    lines.append(f"- OCR service: `{meta['ocr_service_url']}`")
    lines.append(f"- Documents evaluated: {meta['document_count']}")
    lines.append(f"- PSM: {meta['psm']}, low-confidence threshold: {meta['low_confidence_threshold']}%")
    lines.append(
        "- Diagnostics source: "
        + (
            "ocr_service's own modules (zero-drift)"
            if meta["reused_service_modules_for_diagnostics"]
            else "local reimplementation (ocr_service modules not importable - see README.md)"
        )
    )
    lines.append("")

    header = (
        "| Group | Docs | Errors | Native | OCR | Avg time | Avg conf | Low conf% | No structure% | Near-empty% |"
    )
    sep = "|---|---|---|---|---|---|---|---|---|---|"

    lines.append("## Aggregate")
    lines.append("")
    lines.append(header)
    lines.append(sep)
    lines.append(_stats_table_row("**Overall**", agg["overall"]))
    for cat, s in agg["by_category"].items():
        lines.append(_stats_table_row(cat, s))
    lines.append("")

    lines.append("## Per-document detail")
    lines.append("")
    by_category: dict[str, list[DocResult]] = defaultdict(list)
    for r in results:
        by_category[r.category].append(r)

    for cat in sorted(by_category):
        lines.append(f"### {cat}")
        lines.append("")
        lines.append("| File | Path taken | Time | Confidence | Text len | Sections | Tables | Flags |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for r in sorted(by_category[cat], key=lambda r: r.filename):
            if r.error:
                lines.append(f"| {r.relative_path} | error | - | - | - | - | - | `{r.error}` |")
                continue
            flags_str = ", ".join(r.flags) if r.flags else "-"
            lines.append(
                f"| {r.relative_path} | {r.path_taken} | {_fmt(r.response_time_seconds, 's', 2)} "
                f"| {_fmt(r.avg_confidence, '%')} | {r.text_length} | {r.sections_detected} "
                f"| {r.tables_detected} | {flags_str} |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--samples-dir",
        default=os.environ.get("OCR_EVAL_SAMPLES_DIR"),
        help="Directory of sample documents. Must live outside the sense_tool repo. "
        "Can also be set via OCR_EVAL_SAMPLES_DIR.",
    )
    parser.add_argument(
        "--ocr-url",
        default=os.environ.get("OCR_SERVICE_URL", "http://localhost:8001"),
        help="Base URL of the running ocr_service (default: http://localhost:8001, "
        "or OCR_SERVICE_URL env var).",
    )
    parser.add_argument("--psm", type=int, default=6, help="Tesseract page segmentation mode (default: 6).")
    parser.add_argument(
        "--low-confidence-threshold",
        type=float,
        default=60.0,
        help="Flag documents with avg word confidence below this percent (default: 60).",
    )
    parser.add_argument(
        "--min-text-chars",
        type=int,
        default=20,
        help="Flag documents whose extracted text (stripped) is shorter than this as near-empty (default: 20).",
    )
    parser.add_argument("--format", choices=["markdown", "json"], default="markdown")
    parser.add_argument(
        "--output",
        default=None,
        help="Write the report to this path instead of stdout. Refused if the path is inside "
        "the sense_tool repo and not under ocr_service_eval/reports/ (gitignored) - reports can "
        "contain extracted patient text. Omit to print to stdout only.",
    )
    args = parser.parse_args()

    if not args.samples_dir:
        parser.error("--samples-dir is required (or set OCR_EVAL_SAMPLES_DIR)")
    samples_dir = Path(args.samples_dir).expanduser().resolve()
    if not samples_dir.is_dir():
        parser.error(f"samples dir not found: {samples_dir}")

    # Defense-in-depth: refuse to run against sample data that lives inside
    # this repo, even if someone points --samples-dir at it directly.
    try:
        samples_dir.relative_to(_REPO_ROOT)
        parser.error(
            f"--samples-dir ({samples_dir}) is inside the sense_tool repo. Sample documents "
            "must live outside the repo entirely - see ocr_service_eval/README.md."
        )
    except ValueError:
        pass  # good: not inside the repo

    files = discover_samples(samples_dir)
    if not files:
        print(f"No supported sample files found under {samples_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Evaluating {len(files)} document(s) against {args.ocr_url} ...", file=sys.stderr)
    if not REUSING_SERVICE_MODULES:
        print(
            "Note: ocr_service's internal modules aren't importable - using a local "
            "reimplementation for path/confidence diagnostics (see README.md for the "
            "docker-run invocation that avoids this).",
            file=sys.stderr,
        )

    results: list[DocResult] = []
    for i, f in enumerate(files, 1):
        rel = f.relative_to(samples_dir)
        print(f"  [{i}/{len(files)}] {rel} ... ", file=sys.stderr, end="", flush=True)
        result = evaluate_document(
            f, samples_dir, args.ocr_url, args.psm, args.low_confidence_threshold, args.min_text_chars
        )
        print("ERROR" if result.error else (", ".join(result.flags) or "ok"), file=sys.stderr)
        results.append(result)

    agg = aggregate(results)
    meta = {
        "samples_dir": str(samples_dir),
        "ocr_service_url": args.ocr_url,
        "document_count": len(results),
        "psm": args.psm,
        "low_confidence_threshold": args.low_confidence_threshold,
        "min_text_chars": args.min_text_chars,
        "reused_service_modules_for_diagnostics": REUSING_SERVICE_MODULES,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    report = render_markdown(results, agg, meta) if args.format == "markdown" else render_json(results, agg, meta)

    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        reports_dir = (Path(__file__).resolve().parent / "reports").resolve()
        try:
            output_path.relative_to(_REPO_ROOT)
            inside_repo = True
        except ValueError:
            inside_repo = False
        if inside_repo:
            try:
                output_path.relative_to(reports_dir)
            except ValueError:
                parser.error(
                    f"--output ({output_path}) is inside the sense_tool repo but not under "
                    f"the gitignored {reports_dir}. Reports can contain extracted patient text "
                    "and must not risk being committed - write outside the repo, under "
                    "ocr_service_eval/reports/, or omit --output to print to stdout only."
                )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        print(f"Report written to {output_path}", file=sys.stderr)
    else:
        print(report)


if __name__ == "__main__":
    main()
