"""Entity matches: how each name in an extracted claim was matched (ADR 0015, part 4).

Adds `entity_match`, with row-level security on (ADR 0011).

Revision ID: d7365e9f5849
Revises: 6b3ab8320926
Create Date: 2026-10-17 22:00:10.857812
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d7365e9f5849"
down_revision: str | None = "6b3ab8320926"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "entity_match",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("claim_id", sa.Uuid(), nullable=False, comment="The claim the name comes from."),
        sa.Column("entity_type", sa.String(length=7), nullable=False, comment="Company, drug, or trial."),
        sa.Column(
            "name",
            sa.String(length=300),
            nullable=False,
            comment="The name as the claim gives it, or the source link for a company.",
        ),
        sa.Column(
            "status",
            sa.String(length=9),
            nullable=False,
            comment="Resolved (one record), ambiguous (several), or unknown (none).",
        ),
        sa.Column("matched_id", sa.Uuid(), nullable=True, comment="The record, when resolved."),
        sa.Column(
            "candidates",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
            comment="Every candidate record's id, as a list; empty when unknown.",
        ),
        sa.Column(
            "matched_by",
            sa.String(length=100),
            nullable=True,
            comment="Which rule found the candidates, for example 'cik' or 'code_name'.",
        ),
        sa.Column(
            "resolved_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When this match was last made.",
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (matched_id IS NOT NULL)",
            name=op.f("ck_entity_match_resolved_has_record_0c6debd3"),
        ),
        sa.CheckConstraint(
            "entity_type IN ('company', 'drug', 'trial')",
            name=op.f("ck_entity_match_entity_type_allowed_d303eb40"),
        ),
        sa.CheckConstraint(
            "status IN ('resolved', 'ambiguous', 'unknown')",
            name=op.f("ck_entity_match_status_allowed_9ec7dd5d"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name=op.f("fk_entity_match_claim_id_claim"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_match")),
        sa.UniqueConstraint(
            "claim_id", "entity_type", "name", name=op.f("uq_entity_match_claim_id_entity_type_name")
        ),
        comment="How one name in a claim was matched to a company, drug, or trial (ADR 0015, part 4).",
    )
    op.execute('ALTER TABLE "entity_match" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_table("entity_match")
