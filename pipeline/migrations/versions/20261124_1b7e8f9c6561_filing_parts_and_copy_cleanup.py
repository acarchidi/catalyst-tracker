"""Filing parts and copy cleanup: long filings are read part by part, and read copies can go (ADR 0066).

Adds `document_part_reading`, with row-level security on (ADR 0011): one row per part of a 10-Q,
10-K, or 8-K exhibit that a reader has read, so a stopped run goes on where it stopped. Adds two
empty columns to `source_document`: `fully_read_at`, set once every part is read, and
`copy_deleted_at`, set when the stored copy of a fully read document that backs no claim is
deleted. A copy is never marked deleted before its document is fully read (a check).

The downgrade refuses while any copy is marked deleted: without the column, older code would take
each such copy for a lost one (`snapshots-verify` would list it as missing). Otherwise it drops the
table and the two columns; older code never reads filings, so nothing is lost but the progress.

Revision ID: 1b7e8f9c6561
Revises: a64c0de5e7a1
Create Date: 2026-11-24 13:52:56.420000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "1b7e8f9c6561"
down_revision: str | None = "a64c0de5e7a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DELETED_COPIES_REFUSAL = (
    "some stored copies are marked deleted (source_document.copy_deleted_at); older code would report "
    "them as missing, so this downgrade stops here"
)


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "document_part_reading",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("source_document_id", sa.Uuid(), nullable=False, comment="The document the part is from."),
        sa.Column(
            "agent", sa.String(length=100), nullable=False, comment="Which reader, for example 'extractor'."
        ),
        sa.Column("prompt_id", sa.String(length=100), nullable=False, comment="Prompt used."),
        sa.Column("prompt_version", sa.String(length=100), nullable=False, comment="Version of that prompt."),
        sa.Column("model", sa.String(length=100), nullable=False, comment="Model that answered."),
        sa.Column(
            "section",
            sa.String(length=100),
            nullable=False,
            comment="Which section the part is from, for example 'Management's discussion and analysis'.",
        ),
        sa.Column(
            "part",
            sa.BigInteger(),
            nullable=False,
            comment="The part's place among the document's parts, from 0.",
        ),
        sa.Column(
            "parts", sa.BigInteger(), nullable=False, comment="How many parts the document has to read."
        ),
        sa.Column(
            "part_start",
            sa.BigInteger(),
            nullable=False,
            comment="Where the part starts in the document's visible text, in characters.",
        ),
        sa.Column(
            "part_end", sa.BigInteger(), nullable=False, comment="Where the part ends in the visible text."
        ),
        sa.Column(
            "part_sha256",
            sa.String(length=64),
            nullable=False,
            comment="SHA-256 of the part's words, lowercase hex.",
        ),
        sa.Column(
            "catalysts",
            sa.BigInteger(),
            nullable=False,
            comment="How many catalysts the reader proposed in it.",
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=False, comment="When the part was read."),
        sa.CheckConstraint(
            "catalysts >= 0", name=op.f("ck_document_part_reading_catalysts_not_negative_0cdb8aa7")
        ),
        sa.CheckConstraint(
            "part >= 0 AND part < parts", name=op.f("ck_document_part_reading_part_in_range_c0930e06")
        ),
        sa.CheckConstraint(
            "part_start >= 0 AND part_end > part_start",
            name=op.f("ck_document_part_reading_forward_span_d079ef4e"),
        ),
        sa.ForeignKeyConstraint(
            ["source_document_id"],
            ["source_document.id"],
            name=op.f("fk_document_part_reading_source_document_id_source_document"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_part_reading")),
        sa.UniqueConstraint(
            "source_document_id",
            "agent",
            "prompt_id",
            "prompt_version",
            "part_sha256",
            name=op.f(
                "uq_document_part_reading_source_document_id_agent_prompt_id_prompt_version_part_sha256"
            ),
        ),
        comment="One reader's pass over one part of a long filing or exhibit (ADR 0066).",
    )
    op.execute('ALTER TABLE "document_part_reading" ENABLE ROW LEVEL SECURITY')
    op.add_column(
        "source_document",
        sa.Column(
            "fully_read_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the extractor finished reading every part of this filing or exhibit (ADR 0066); it is "
                "never read again. Empty until then, and for press releases."
            ),
        ),
    )
    op.add_column(
        "source_document",
        sa.Column(
            "copy_deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment=(
                "When the stored copy was deleted, because the document was fully read and backs no claim "
                "(ADR 0066). Empty while the copy is kept."
            ),
        ),
    )
    op.create_check_constraint(
        op.f("ck_source_document_deleted_only_after_read_898d363b"),
        "source_document",
        "copy_deleted_at IS NULL OR fully_read_at IS NOT NULL",
    )


def downgrade() -> None:
    """Reverse this migration, unless a copy is already marked deleted."""
    deleted = op.get_bind().execute(
        sa.text("SELECT count(*) FROM source_document WHERE copy_deleted_at IS NOT NULL")
    )
    if deleted.scalar_one():
        raise RuntimeError(DELETED_COPIES_REFUSAL)
    op.drop_constraint(
        op.f("ck_source_document_deleted_only_after_read_898d363b"), "source_document", type_="check"
    )
    op.drop_column("source_document", "copy_deleted_at")
    op.drop_column("source_document", "fully_read_at")
    op.drop_table("document_part_reading")
