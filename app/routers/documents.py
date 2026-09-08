import uuid
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.enums import DocumentStatus
from app.models import Document
from app.schemas import DocumentIntakeResponse, DocumentOut, DocumentSummary, SearchResponse
from app.services import export
from app.services.search import search_documents
from app.storage import get_storage_backend

router = APIRouter(prefix="/documents", tags=["documents"])


@router.post(
    "/intake",
    response_model=DocumentIntakeResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def intake_document(
    request: Request,
    source: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
) -> DocumentIntakeResponse:
    settings = get_settings()

    data = await file.read()
    if not data:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    if len(data) > settings.max_upload_size_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds max size of {settings.max_upload_size_bytes} bytes",
        )

    document_id = uuid.uuid4()
    storage = get_storage_backend()
    key = storage.build_key(document_id, file.filename or "upload")
    storage.save(key, data)

    document = Document(
        id=document_id,
        source=source,
        status=DocumentStatus.QUEUED,
        raw_file_path=key,
    )
    db.add(document)
    await db.commit()
    await db.refresh(document)

    await request.app.state.arq_pool.enqueue_job("run_ocr_extraction", str(document.id))

    return DocumentIntakeResponse(id=document.id, status=document.status, source=document.source)


@router.get("/search", response_model=SearchResponse)
async def search(
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    rows = await search_documents(db, q, limit=limit, offset=offset)
    results = [
        DocumentSummary(
            id=doc.id,
            source=doc.source,
            status=doc.status,
            created_at=doc.created_at,
            updated_at=doc.updated_at,
            rank=rank,
            snippet=snippet,
        )
        for doc, rank, snippet in rows
    ]
    return SearchResponse(query=q, count=len(results), results=results)


@router.get("/{document_id}", response_model=DocumentOut)
async def get_document(document_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> DocumentOut:
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return DocumentOut.model_validate(document)


@router.get("/{document_id}/export")
async def export_document(
    document_id: uuid.UUID,
    format: Literal["pdf", "docx", "xlsx"] = Query(...),
    db: AsyncSession = Depends(get_db),
) -> Response:
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if document.status != DocumentStatus.DONE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document is not ready for export (status={document.status.value})",
        )

    try:
        content = export.render(format, document.source, document.structured_data)
    except export.ExportError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    filename = f"{document.id}.{format}"
    return Response(
        content=content,
        media_type=export.content_type_for(format),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
