from fastapi import FastAPI, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel

from app.ocr import OCRError, extract_text_from_bytes

app = FastAPI(title="Sense_tool OCR Service")


class OCRResponse(BaseModel):
    text: str
    # Raw pdfplumber shape (list of tables, each a list of rows, each row a
    # list of nullable cell strings). Only ever populated for the
    # native-PDF-text path; empty for the OCR/rasterized path and for
    # native PDFs with no detected tables. Left in this raw shape on
    # purpose - Sense_tool's structure-parsing step decides how to fold it
    # into structured_data, this service doesn't know about that format.
    tables: list[list[list[str | None]]] = []


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ocr", response_model=OCRResponse)
async def ocr(
    file: UploadFile = File(...),
    psm: int = Query(6, ge=0, le=13, description="Tesseract page segmentation mode"),
) -> OCRResponse:
    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")

    try:
        result = extract_text_from_bytes(file.filename or "upload", data, psm=psm)
    except OCRError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    return OCRResponse(text=result.text, tables=result.tables)
