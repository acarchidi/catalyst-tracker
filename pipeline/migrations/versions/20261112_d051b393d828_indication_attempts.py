"""Indication attempts: each try at a published catalyst's indication (ADR 0053, decision 2).

Adds `indication_attempt`, with row-level security on (ADR 0011). A row is kept whether the two
readers found an indication or not, so a re-run never pays to ask again. The downgrade drops the
rows: they record model calls, and the next run asks again.

Revision ID: d051b393d828
Revises: 3798b407da9a
Create Date: 2026-11-12 17:18:19.876932
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d051b393d828"
down_revision: str | None = "3798b407da9a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "indication_attempt",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("catalyst_id", sa.Uuid(), nullable=False, comment="The published catalyst."),
        sa.Column(
            "claim_id",
            sa.Uuid(),
            nullable=False,
            comment="The catalyst's source claim whose document and quote the readers read.",
        ),
        sa.Column("outcome", sa.String(length=7), nullable=False, comment="Found, or refused with a reason."),
        sa.Column(
            "reason",
            sa.String(length=300),
            nullable=True,
            comment="Why no indication stands; empty when found.",
        ),
        sa.Column(
            "indication_claim_id",
            sa.Uuid(),
            nullable=True,
            comment="The claim holding the indication's sentence, when found.",
        ),
        sa.Column(
            "reader_prompt_version",
            sa.String(length=100),
            nullable=False,
            comment="Version of the first reader's prompt.",
        ),
        sa.Column(
            "checker_prompt_version",
            sa.String(length=100),
            nullable=False,
            comment="Version of the second reader's prompt.",
        ),
        sa.Column(
            "reader_model", sa.String(length=100), nullable=False, comment="Model the first reader ran on."
        ),
        sa.Column(
            "checker_model", sa.String(length=100), nullable=False, comment="Model the second reader ran on."
        ),
        sa.Column(
            "attempted_at", sa.DateTime(timezone=True), nullable=False, comment="When the readers were asked."
        ),
        sa.CheckConstraint(
            "(outcome = 'found') = (indication_claim_id IS NOT NULL)",
            name=op.f("ck_indication_attempt_found_has_claim_dd430369"),
        ),
        sa.CheckConstraint(
            "outcome <> 'refused' OR reason IS NOT NULL",
            name=op.f("ck_indication_attempt_refused_has_reason_89693f63"),
        ),
        sa.CheckConstraint(
            "outcome IN ('found', 'refused')", name=op.f("ck_indication_attempt_outcome_allowed_170aa8c6")
        ),
        sa.ForeignKeyConstraint(
            ["catalyst_id"],
            ["catalyst.id"],
            name=op.f("fk_indication_attempt_catalyst_id_catalyst"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name=op.f("fk_indication_attempt_claim_id_claim"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["indication_claim_id"],
            ["claim.id"],
            name=op.f("fk_indication_attempt_indication_claim_id_claim"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_indication_attempt")),
        sa.UniqueConstraint(
            "catalyst_id",
            "claim_id",
            "reader_prompt_version",
            "checker_prompt_version",
            "reader_model",
            "checker_model",
            name=op.f(
                "uq_indication_attempt_catalyst_id_claim_id_reader_prompt_version_checker_prompt_version_reader_model_checker_model"
            ),
        ),
        comment="One try at a published catalyst's indication by both readers (ADR 0053, decision 2).",
    )
    op.execute('ALTER TABLE "indication_attempt" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_table("indication_attempt")
