import uuid
from datetime import datetime

from sqlalchemy import Computed, DateTime, Enum, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.enums import DocumentStatus


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        nullable=False,
        default=DocumentStatus.QUEUED,
        server_default=DocumentStatus.QUEUED.value,
    )
    raw_file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    structured_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # WP-B: metadata for extracted image/photo/chart regions. A JSON array of
    # {bbox, bbox_space, page, source, region_type_guess, storage_key,
    # width, height, format}. The crop bytes themselves live in the storage
    # backend at `storage_key`; only the metadata is in the DB. Populated by
    # the OCR worker step; NOT consumed by structure parsing or export yet
    # (that is a later work package).
    image_regions: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # WP-G: how `extracted_text` was produced and how the document exports as
    # a pixel-perfect PDF.
    #   text_source        "native_pdf" -> export streams the stored original
    #                      file verbatim; "ocr" -> export streams the
    #                      searchable PDF at `searchable_pdf_key`. NULL for
    #                      documents processed before WP-G (export falls back
    #                      to the legacy structured-text PDF).
    #   ocr_lang           Tesseract language string used for the OCR run
    #                      ("eng", "eng+guj", ...). Kept so the searchable
    #                      PDF's invisible text layer matches the OCR run.
    #   searchable_pdf_key storage key of the pre-generated searchable PDF
    #                      (built in the OCR worker step). NULL on the
    #                      native-PDF path and for pre-WP-G documents.
    text_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ocr_lang: Mapped[str | None] = mapped_column(String(32), nullable=True)
    searchable_pdf_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Generated/stored column powering full-text search (GIN-indexed in the migration).
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(extracted_text, ''))", persisted=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return f"<Document id={self.id} status={self.status}>"
