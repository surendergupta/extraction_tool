import pytest
import uuid

import httpx

from app.config import get_settings
from app.enums import DocumentStatus
from app.services.ocr import OCRError, OcrResult
from app.worker import run_ocr_extraction, run_structure_parsing

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_ocr_step_moves_queued_to_processing_and_extracts_text(
    make_document, worker_ctx, monkeypatch
):
    document = await make_document(status=DocumentStatus.QUEUED)

    async def fake_extract_text(filename: str, data: bytes) -> OcrResult:
        return OcrResult(text="PATIENT HISTORY:\nNo prior admissions.")

    monkeypatch.setattr("app.worker.ocr.extract_text", fake_extract_text)

    await run_ocr_extraction(worker_ctx, str(document.id))

    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.PROCESSING
        assert refreshed.extracted_text == "PATIENT HISTORY:\nNo prior admissions."
        assert refreshed.error_message is None

    queued = await worker_ctx["redis"].queued_jobs()
    assert "run_structure_parsing" in [j.function for j in queued]


async def test_structure_step_populates_structured_data_and_marks_done(make_document, worker_ctx):
    document = await make_document(
        status=DocumentStatus.PROCESSING,
        extracted_text="DIAGNOSIS:\nMild hypertension.\n\nLABS\nTest  |  Result\nWBC   |  7.2",
    )

    await run_structure_parsing(worker_ctx, str(document.id))

    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.DONE
        assert refreshed.error_message is None
        assert refreshed.structured_data is not None
        titles = [s["title"] for s in refreshed.structured_data["sections"]]
        assert "DIAGNOSIS" in titles
        assert len(refreshed.structured_data["tables"]) == 1


async def test_full_pipeline_queued_to_done(make_document, worker_ctx, monkeypatch):
    document = await make_document(status=DocumentStatus.QUEUED)

    async def fake_extract_text(filename: str, data: bytes) -> OcrResult:
        return OcrResult(text="NOTES:\nEverything looks normal.")

    monkeypatch.setattr("app.worker.ocr.extract_text", fake_extract_text)

    await run_ocr_extraction(worker_ctx, str(document.id))
    await run_structure_parsing(worker_ctx, str(document.id))

    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.DONE
        assert refreshed.extracted_text == "NOTES:\nEverything looks normal."
        assert refreshed.structured_data["sections"][0]["title"] == "NOTES"


async def test_native_pdf_tables_flow_through_to_structured_data(
    make_document, worker_ctx, monkeypatch
):
    """pdfplumber's extract_tables() output (native-PDF-text path) must
    ride along the job chain and populate structured_data's tables
    directly, bypassing the space-heuristic detector for that content."""
    document = await make_document(status=DocumentStatus.QUEUED)

    native_table = [
        ["Test Name", "Status", "Result", "Reference Interval", "Unit"],
        ["Haemoglobin", None, "11.4", "12.0-15.0", "g/dL"],
        ["WBC", "L", "9.6", "4-10", "10^3/mm3"],
    ]

    async def fake_extract_text(filename: str, data: bytes) -> OcrResult:
        return OcrResult(text="LAB REPORT:\nSee results below.", tables=[native_table])

    monkeypatch.setattr("app.worker.ocr.extract_text", fake_extract_text)

    await run_ocr_extraction(worker_ctx, str(document.id))

    # the OCR step must hand pdfplumber's tables to the next job, not drop them
    queued = await worker_ctx["redis"].queued_jobs()
    structure_job = next(j for j in queued if j.function == "run_structure_parsing")
    assert structure_job.args == (str(document.id), [native_table])

    await run_structure_parsing(worker_ctx, str(document.id), [native_table])

    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.DONE
        tables = refreshed.structured_data["tables"]
        assert len(tables) == 1
        assert tables[0]["source"] == "native_pdf"
        assert tables[0]["rows"][0] == [
            "Test Name",
            "Status",
            "Result",
            "Reference Interval",
            "Unit",
        ]
        assert tables[0]["rows"][1] == ["Haemoglobin", "", "11.4", "12.0-15.0", "g/dL"]
        assert tables[0]["rows"][2] == ["WBC", "L", "9.6", "4-10", "10^3/mm3"]


async def test_ocr_failure_marks_document_failed_and_never_leaves_it_processing(
    make_document, worker_ctx, monkeypatch
):
    document = await make_document(status=DocumentStatus.QUEUED)

    async def boom(filename: str, data: bytes) -> str:
        raise OCRError("tesseract exploded")

    monkeypatch.setattr("app.worker.ocr.extract_text", boom)

    await run_ocr_extraction(worker_ctx, str(document.id))

    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.FAILED
        assert "tesseract exploded" in refreshed.error_message
        assert refreshed.extracted_text is None

    # no follow-up job should have been enqueued for a failed OCR step
    queued = await worker_ctx["redis"].queued_jobs()
    assert "run_structure_parsing" not in [j.function for j in queued]


async def test_ocr_timeout_marks_document_failed_with_clear_message(
    make_document, worker_ctx, monkeypatch
):
    """A slow/hanging OCR service response must not propagate as an
    unhandled exception or leave the document stuck in PROCESSING."""
    document = await make_document(status=DocumentStatus.QUEUED)

    async def hangs_then_times_out(filename: str, data: bytes) -> str:
        raise httpx.ReadTimeout("simulated slow OCR service response")

    monkeypatch.setattr("app.worker.ocr.extract_text", hangs_then_times_out)

    await run_ocr_extraction(worker_ctx, str(document.id))

    expected_seconds = get_settings().ocr_read_timeout_seconds
    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.FAILED
        assert refreshed.error_message == f"OCR service timed out after {expected_seconds:g}s"
        assert refreshed.extracted_text is None

    queued = await worker_ctx["redis"].queued_jobs()
    assert "run_structure_parsing" not in [j.function for j in queued]


async def test_structure_failure_marks_document_failed(make_document, worker_ctx, monkeypatch):
    document = await make_document(
        status=DocumentStatus.PROCESSING, extracted_text="some ocr text"
    )

    def boom(text, native_tables=None):
        raise ValueError("bad heuristic input")

    monkeypatch.setattr("app.worker.structure.parse_structure", boom)

    await run_structure_parsing(worker_ctx, str(document.id))

    async with worker_ctx["sessionmaker"]() as session:
        refreshed = await session.get(type(document), document.id)
        assert refreshed.status == DocumentStatus.FAILED
        assert "bad heuristic input" in refreshed.error_message


async def test_worker_steps_are_noop_for_unknown_document_id(worker_ctx):
    missing_id = str(uuid.uuid4())
    # should not raise even though the document does not exist
    await run_ocr_extraction(worker_ctx, missing_id)
    await run_structure_parsing(worker_ctx, missing_id)
