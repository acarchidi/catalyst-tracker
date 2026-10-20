"""Trial sizes from the registry (ADR 0039; owner request 2026-10-20).

Adds `trial.enrollment` and `trial.enrollment_type`, both empty until the next ClinicalTrials.gov
run fills them. The downgrade drops them: they are a copy of registry values, kept like the
phase, and the next registry run after an upgrade fills them again.

Revision ID: b3f1c7e2a9d4
Revises: 5e7a0c3b9d21
Create Date: 2026-10-20 02:11:36.677966
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3f1c7e2a9d4"
down_revision: str | None = "5e7a0c3b9d21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

NOT_NEGATIVE = "ck_trial_enrollment_not_negative_703e2828"
TYPE_ALLOWED = "ck_trial_enrollment_type_allowed_9fb237b2"


def upgrade() -> None:
    """Apply this migration."""
    op.add_column(
        "trial",
        sa.Column(
            "enrollment",
            sa.BigInteger(),
            nullable=True,
            comment="Number of participants as the registry reports it (trial size); registry data, private export only (ADR 0039).",
        ),
    )
    op.add_column(
        "trial",
        sa.Column(
            "enrollment_type",
            sa.String(length=9),
            nullable=True,
            comment="Whether the size is the final count (actual) or the plan (estimated).",
        ),
    )
    op.create_check_constraint(op.f(NOT_NEGATIVE), "trial", "enrollment >= 0")
    op.create_check_constraint(op.f(TYPE_ALLOWED), "trial", "enrollment_type IN ('actual', 'estimated')")


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_constraint(op.f(TYPE_ALLOWED), "trial", type_="check")
    op.drop_constraint(op.f(NOT_NEGATIVE), "trial", type_="check")
    op.drop_column("trial", "enrollment_type")
    op.drop_column("trial", "enrollment")
