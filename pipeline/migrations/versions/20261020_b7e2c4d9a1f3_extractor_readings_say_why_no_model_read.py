"""Extractor readings say why no model read a document (ADR 0038).

Adds `document_reading.skipped`: empty when a model read the document, else why none did (not a
web page or plain text, no readable text, or over the role's input limit). The downgrade deletes
the skipped rows first: without the column they would look like a model's reading, and the next
run checks those documents again without a model call.

Revision ID: b7e2c4d9a1f3
Revises: 5e7a0c3b9d21
Create Date: 2026-10-20 02:11:05.357775
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c4d9a1f3"
down_revision: str | None = "5e7a0c3b9d21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SKIPPED_CHECK = "ck_document_reading_skipped_allowed_e4835872"
MODEL_BEFORE = "Model that answered."
MODEL_AFTER = "Model that answered, or for a skipped document the one it was meant for."


def upgrade() -> None:
    """Apply this migration."""
    op.add_column(
        "document_reading",
        sa.Column(
            "skipped",
            sa.String(length=16),
            nullable=True,
            comment="Why no model read the document; empty when a model read it.",
        ),
    )
    op.create_check_constraint(
        op.f(SKIPPED_CHECK), "document_reading", "skipped IN ('not_text', 'no_readable_text', 'too_large')"
    )
    op.alter_column(
        "document_reading",
        "model",
        existing_type=sa.VARCHAR(length=100),
        comment=MODEL_AFTER,
        existing_comment=MODEL_BEFORE,
        existing_nullable=False,
    )


def downgrade() -> None:
    """Reverse this migration."""
    op.execute("DELETE FROM document_reading WHERE skipped IS NOT NULL")
    op.alter_column(
        "document_reading",
        "model",
        existing_type=sa.VARCHAR(length=100),
        comment=MODEL_BEFORE,
        existing_comment=MODEL_AFTER,
        existing_nullable=False,
    )
    op.drop_constraint(op.f(SKIPPED_CHECK), "document_reading", type_="check")
    op.drop_column("document_reading", "skipped")
