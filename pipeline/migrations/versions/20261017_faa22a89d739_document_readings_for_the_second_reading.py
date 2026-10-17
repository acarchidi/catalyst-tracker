"""Document readings: each reader's pass over a source document (ADR 0018, part 2).

Adds `document_reading`, with row-level security on (ADR 0011). A reader that found nothing
still leaves a row, so a re-run does not read the same document again.

Revision ID: faa22a89d739
Revises: f4cde85743cd
Create Date: 2026-10-17 23:03:41.565769
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "faa22a89d739"
down_revision: str | None = "f4cde85743cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "document_reading",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("source_document_id", sa.Uuid(), nullable=False, comment="The document read."),
        sa.Column(
            "agent", sa.String(length=100), nullable=False, comment="Which reader, for example 'verifier'."
        ),
        sa.Column("prompt_id", sa.String(length=100), nullable=False, comment="Prompt used."),
        sa.Column("prompt_version", sa.String(length=100), nullable=False, comment="Version of that prompt."),
        sa.Column("model", sa.String(length=100), nullable=False, comment="Model that answered."),
        sa.Column(
            "catalysts", sa.BigInteger(), nullable=False, comment="How many catalysts the reader proposed."
        ),
        sa.Column(
            "read_at", sa.DateTime(timezone=True), nullable=False, comment="When the document was read."
        ),
        sa.CheckConstraint(
            "catalysts >= 0", name=op.f("ck_document_reading_catalysts_not_negative_0cdb8aa7")
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            name=op.f("fk_document_reading_source_document_id_source_document"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_reading")),
        sa.UniqueConstraint(
            "source_document_id",
            "agent",
            "prompt_id",
            "prompt_version",
            "model",
            name=op.f("uq_document_reading_source_document_id_agent_prompt_id_prompt_version_model"),
        ),
        comment="One reader's pass over one source document, kept even when it found nothing (ADR 0018, part 2).",
    )
    op.execute('ALTER TABLE "document_reading" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_table("document_reading")
