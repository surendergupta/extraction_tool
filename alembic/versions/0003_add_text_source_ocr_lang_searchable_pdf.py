"""add text_source, ocr_lang, searchable_pdf_key to documents (WP-G)

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-09

WP-G (pixel-perfect PDF export) needs to know, at export time, which path
produced a document so it can pick the right PDF strategy:

  - text_source        "native_pdf" | "ocr" - was `extracted_text` pulled
                       from the PDF's own text layer, or produced by
                       rasterize+Tesseract. Native-PDF docs export as a
                       byte-for-byte pass-through of the stored original;
                       OCR docs export as a Tesseract searchable PDF
                       (original page image + invisible text layer).
  - ocr_lang           the Tesseract language string used for the OCR run
                       ("eng", "eng+guj", ...). Persisted so a searchable
                       PDF's invisible text layer can be regenerated with a
                       matching language later. NULL / "eng" for every
                       document today (the pipeline only uses non-default
                       langs on an opt-in basis that nothing wires yet).
  - searchable_pdf_key storage key of the pre-generated searchable PDF
                       (built during the OCR worker step, same single
                       Tesseract pass that produces the text). NULL for
                       native-PDF docs and for anything processed before
                       this migration.

All three are nullable with no backfill: pre-existing rows keep NULL and
the export endpoint falls back to the legacy structured-text PDF for them.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("text_source", sa.String(length=16), nullable=True))
    op.add_column("documents", sa.Column("ocr_lang", sa.String(length=32), nullable=True))
    op.add_column(
        "documents", sa.Column("searchable_pdf_key", sa.String(length=1024), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("documents", "searchable_pdf_key")
    op.drop_column("documents", "ocr_lang")
    op.drop_column("documents", "text_source")
