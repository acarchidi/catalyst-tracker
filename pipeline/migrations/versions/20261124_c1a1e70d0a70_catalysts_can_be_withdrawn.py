"""Catalysts can be withdrawn: a reviewed withdrawal list unpublishes them (ADR 0070).

Widens `catalyst.status` by one letter and allows the value `withdrawn`. No row changes. The
downgrade refuses while any catalyst is withdrawn, since the older code would show it again.

Revision ID: c1a1e70d0a70
Revises: 1b7e8f9c6561
Create Date: 2026-11-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1a1e70d0a70"
down_revision: str | None = "1b7e8f9c6561"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = ("ck_catalyst_status_allowed_b8a8528d", "status IN ('upcoming', 'slipped', 'resolved')")
NEW = ("ck_catalyst_status_allowed_ae034fd6", "status IN ('upcoming', 'slipped', 'resolved', 'withdrawn')")
OLD_COMMENT = "Upcoming, slipped, or resolved."
NEW_COMMENT = "Upcoming, slipped, resolved, or withdrawn after review."


def upgrade() -> None:
    """Apply this migration."""
    op.drop_constraint(op.f(OLD[0]), "catalyst", type_="check")
    op.alter_column(
        "catalyst",
        "status",
        type_=sa.String(length=9),
        existing_type=sa.String(length=8),
        existing_nullable=False,
        comment=NEW_COMMENT,
        existing_comment=OLD_COMMENT,
    )
    op.create_check_constraint(op.f(NEW[0]), "catalyst", NEW[1])


def downgrade() -> None:
    """Revert this migration; refused while a catalyst is withdrawn."""
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM catalyst WHERE status = 'withdrawn') THEN "
        "RAISE EXCEPTION 'a catalyst is withdrawn; the older code would show it again'; END IF; END $$"
    )
    op.drop_constraint(op.f(NEW[0]), "catalyst", type_="check")
    op.alter_column(
        "catalyst",
        "status",
        type_=sa.String(length=8),
        existing_type=sa.String(length=9),
        existing_nullable=False,
        comment=OLD_COMMENT,
        existing_comment=NEW_COMMENT,
    )
    op.create_check_constraint(op.f(OLD[0]), "catalyst", OLD[1])
