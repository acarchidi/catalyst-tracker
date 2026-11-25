"""Estimated dates from the registry (owner decision 2026-11-23; ADR 0067).

`catalyst.catalyst_type` gains `primary_completion` (a trial's estimated primary completion, from
its registry record only) and `catalyst.status` gains `replaced` (an estimate a company-stated
date took the place of). Both are new allowed values, so the upgrade changes no row.

The downgrade refuses while any catalyst holds either value, rather than delete published
records or their history (ADR 0016's rule for migrations that could lose data).

Revision ID: e5d2a7c41b93
Revises: c3d5e7f9a1b2
Create Date: 2026-11-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5d2a7c41b93"
down_revision: str | None = "c3d5e7f9a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES = (
    "'pdufa_date', 'advisory_committee', 'topline_readout', 'conference_presentation', "
    "'regulatory_submission', 'regulatory_acceptance', 'chmp_opinion', 'complete_response_letter', "
    "'label_expansion', 'enrollment_completion', 'offering', 'shelf_filing', 'merger_acquisition', 'earnings'"
)
OLD_TYPE = ("ck_catalyst_catalyst_type_allowed_a07a372f", f"catalyst_type IN ({_TYPES})")
NEW_TYPE = (
    "ck_catalyst_catalyst_type_allowed_4c6f0849",
    f"catalyst_type IN ({_TYPES}, 'primary_completion')",
)
OLD_STATUS = (
    "ck_catalyst_status_allowed_ae034fd6",
    "status IN ('upcoming', 'slipped', 'resolved', 'withdrawn')",
)
NEW_STATUS = (
    "ck_catalyst_status_allowed_820c43bc",
    "status IN ('upcoming', 'slipped', 'resolved', 'replaced', 'withdrawn')",
)
OLD_COMMENT = "Upcoming, slipped, resolved, or withdrawn after review."
NEW_COMMENT = (
    "Upcoming, slipped, resolved, replaced (a registry estimate a company date replaced), "
    "or withdrawn after review."
)


def _swap(old: tuple[str, str], new: tuple[str, str]) -> None:
    op.drop_constraint(op.f(old[0]), "catalyst", type_="check")
    op.create_check_constraint(op.f(new[0]), "catalyst", new[1])


def _comment(comment: str, existing: str) -> None:
    op.alter_column(
        "catalyst",
        "status",
        existing_type=sa.String(length=9),
        existing_nullable=False,
        comment=comment,
        existing_comment=existing,
    )


def upgrade() -> None:
    """Apply this migration."""
    _swap(OLD_TYPE, NEW_TYPE)
    _swap(OLD_STATUS, NEW_STATUS)
    _comment(NEW_COMMENT, OLD_COMMENT)


def downgrade() -> None:
    """Reverse this migration, refusing while any catalyst uses a new value."""
    used = op.get_bind().execute(
        sa.text(
            "SELECT count(*) FROM catalyst WHERE catalyst_type = 'primary_completion' OR status = 'replaced'"
        )
    )
    if used.scalar_one():
        raise RuntimeError(
            "catalysts hold estimated dates from the registry (ADR 0067); downgrading would lose them"
        )
    _comment(OLD_COMMENT, NEW_COMMENT)
    _swap(NEW_STATUS, OLD_STATUS)
    _swap(NEW_TYPE, OLD_TYPE)
