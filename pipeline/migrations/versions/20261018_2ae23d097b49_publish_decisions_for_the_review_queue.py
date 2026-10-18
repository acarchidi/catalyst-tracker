"""Publish decisions: the gate's latest decision on each extractor claim (ADR 0018, part 5a).

Adds `claim_decision`, with row-level security on (ADR 0011). Each claim keeps one row: publish,
or review with every reason. The rows are recomputed from stored checks and stored copies by
`catalyst decide-claims`, so the downgrade may drop them.

Revision ID: 2ae23d097b49
Revises: faa22a89d739
Create Date: 2026-10-18 00:57:14.157802
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "2ae23d097b49"
down_revision: str | None = "faa22a89d739"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "claim_decision",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("claim_id", sa.Uuid(), nullable=False, comment="The claim decided."),
        sa.Column(
            "decision", sa.String(length=7), nullable=False, comment="Publish, or wait in the review queue."
        ),
        sa.Column(
            "reasons",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
            comment="Every reason the claim waits for review, in plain words; empty when it may publish.",
        ),
        sa.Column(
            "decided_at", sa.DateTime(timezone=True), nullable=False, comment="When the gate last decided."
        ),
        sa.CheckConstraint(
            "CASE WHEN jsonb_typeof(reasons) = 'array' THEN (decision = 'review') = (jsonb_array_length(reasons) > 0) ELSE false END",
            name=op.f("ck_claim_decision_review_has_reasons_4019f7b0"),
        ),
        sa.CheckConstraint(
            "decision IN ('publish', 'review')", name=op.f("ck_claim_decision_decision_allowed_d954cda8")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name=op.f("fk_claim_decision_claim_id_claim"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_decision")),
        sa.UniqueConstraint("claim_id", name=op.f("uq_claim_decision_claim_id")),
        comment="The publish gate's latest decision on one extractor claim, with every reason it waits for review.",
    )
    op.execute('ALTER TABLE "claim_decision" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_table("claim_decision")
