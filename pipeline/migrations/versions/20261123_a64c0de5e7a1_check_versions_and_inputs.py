"""Check versions and inputs: each stored check outcome says which code found it and from what (ADR 0064).

Adds two empty columns to `claim_check`. A row without them is checked again on the next run,
once, so the upgrade needs no data change. The downgrade drops them; the outcomes stay, and the
code before this revision checks every claim every run, as it always did.

Revision ID: a64c0de5e7a1
Revises: d051b393d828
Create Date: 2026-11-23 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a64c0de5e7a1"
down_revision: str | None = "d051b393d828"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.add_column(
        "claim_check",
        sa.Column(
            "rule_version",
            sa.String(length=16),
            nullable=True,
            comment=(
                "Version of the check's code that found the outcome (ADR 0064); new code checks the claim "
                "again. Empty on rows from before versions were kept."
            ),
        ),
    )
    op.add_column(
        "claim_check",
        sa.Column(
            "input_digest",
            sa.String(length=64),
            nullable=True,
            comment=(
                "SHA-256 of everything else the check read for this claim (ADR 0064); a run skips the claim "
                "only while this and the code version are unchanged. Empty when the claim must be checked "
                "every run."
            ),
        ),
    )


def downgrade() -> None:
    """Revert this migration."""
    op.drop_column("claim_check", "input_digest")
    op.drop_column("claim_check", "rule_version")
