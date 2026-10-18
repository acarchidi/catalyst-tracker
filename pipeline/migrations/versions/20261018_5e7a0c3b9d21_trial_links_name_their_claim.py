"""Trial links name their claim (ADR 0018, part 5b; ADR 0025, rule 3).

Adds a required `catalyst_trial.claim_id`, a foreign key to `claim`, so a trial link always has a
quote behind it, as drug links do. Each step refuses to run when it would lose stored data: the
upgrade while a trial link has no claim to name, the downgrade while a link would lose its claim.

Revision ID: 5e7a0c3b9d21
Revises: 2ae23d097b49
Create Date: 2026-10-18 02:46:32.469441
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e7a0c3b9d21"
down_revision: str | None = "2ae23d097b49"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FOREIGN_KEY = "fk_catalyst_trial_claim_id_claim"


def _refuse_if_links(message: str) -> None:
    if op.get_bind().execute(sa.text("SELECT count(*) FROM catalyst_trial")).scalar_one():
        raise RuntimeError(message)


def upgrade() -> None:
    """Apply this migration."""
    _refuse_if_links("catalyst_trial has trial links without a claim; nothing can name one for them")
    op.add_column(
        "catalyst_trial",
        sa.Column(
            "claim_id",
            sa.Uuid(),
            nullable=False,
            comment="The claim whose quote names this trial for this catalyst.",
        ),
    )
    op.create_foreign_key(
        op.f(FOREIGN_KEY), "catalyst_trial", "claim", ["claim_id"], ["id"], ondelete="RESTRICT"
    )


def downgrade() -> None:
    """Reverse this migration."""
    _refuse_if_links("catalyst_trial has trial links that would lose the claim that states them")
    op.drop_constraint(op.f(FOREIGN_KEY), "catalyst_trial", type_="foreignkey")
    op.drop_column("catalyst_trial", "claim_id")
