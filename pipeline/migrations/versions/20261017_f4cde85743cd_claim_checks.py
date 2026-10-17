"""Claim checks: each verification check's latest outcome per claim (ADR 0018).

Adds `claim_check`, with row-level security on (ADR 0011).

Revision ID: f4cde85743cd
Revises: d7365e9f5849
Create Date: 2026-10-17 22:20:42.944923
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4cde85743cd"
down_revision: str | None = "d7365e9f5849"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "claim_check",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("claim_id", sa.Uuid(), nullable=False, comment="The claim checked."),
        sa.Column(
            "check_name",
            sa.String(length=100),
            nullable=False,
            comment="Which check, for example 'window_not_past'.",
        ),
        sa.Column("outcome", sa.String(length=14), nullable=False, comment="Pass, fail, or not applicable."),
        sa.Column(
            "reason",
            sa.String(length=300),
            nullable=True,
            comment="Why it failed or did not apply, in plain words.",
        ),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, comment="When the check last ran."
        ),
        sa.CheckConstraint(
            "outcome <> 'fail' OR reason IS NOT NULL", name=op.f("ck_claim_check_fail_has_reason_bde5d2fc")
        ),
        sa.CheckConstraint(
            "outcome IN ('pass', 'fail', 'not_applicable')",
            name=op.f("ck_claim_check_outcome_allowed_2dba5582"),
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name=op.f("fk_claim_check_claim_id_claim"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_claim_check")),
        sa.UniqueConstraint("claim_id", "check_name", name=op.f("uq_claim_check_claim_id_check_name")),
        comment="One verification check's outcome for one claim (spec: Verification; ADR 0018).",
    )
    op.execute('ALTER TABLE "claim_check" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_table("claim_check")
