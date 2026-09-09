"""add image_regions to documents (WP-B)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-09

Adds a nullable JSONB column holding metadata for extracted image/photo/chart
regions (bbox, page, source, best-effort type guess, and the storage key of
the crop bytes). Detection + extraction only - nothing consumes this column
for structure parsing or export yet.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("image_regions", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "image_regions")
