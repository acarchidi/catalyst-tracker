"""Duplicate entries can be merged: one event listed twice becomes one entry (ADR 0081).

`catalyst.status` gains `merged` (an entry kept in another's place as the same event), and the new
table `catalyst_merged_source` links the kept entry to each claim of the merged one, so those claims
show as its extra sources. Row-level security is on, like every table (ADR 0011). The upgrade
changes no row.

The downgrade refuses while any catalyst is merged or any link exists: the older code would show
both entries again and lose the record of the merge (ADR 0016's rule for migrations that could lose
data).

Revision ID: f2b9d4c6a8e1
Revises: e5d2a7c41b93
Create Date: 2026-11-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2b9d4c6a8e1"
down_revision: str | None = "e5d2a7c41b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = "'upcoming', 'slipped', 'resolved', 'replaced', 'withdrawn'"
OLD_STATUS = ("ck_catalyst_status_allowed_820c43bc", f"status IN ({_STATUSES})")
NEW_STATUS = ("ck_catalyst_status_allowed_7013301c", f"status IN ({_STATUSES}, 'merged')")
OLD_COMMENT = (
    "Upcoming, slipped, resolved, replaced (a registry estimate a company date replaced), "
    "or withdrawn after review."
)
NEW_COMMENT = (
    "Upcoming, slipped, resolved, replaced (a registry estimate a company date replaced), "
    "withdrawn after review, or merged (the same event as another entry, kept in its place)."
)
TABLE = "catalyst_merged_source"


def _status(old: tuple[str, str], new: tuple[str, str], comment: str, existing: str) -> None:
    op.drop_constraint(op.f(old[0]), "catalyst", type_="check")
    op.create_check_constraint(op.f(new[0]), "catalyst", new[1])
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
    _status(OLD_STATUS, NEW_STATUS, NEW_COMMENT, OLD_COMMENT)
    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("catalyst_id", sa.Uuid(), nullable=False, comment="The entry kept."),
        sa.Column("merged_id", sa.Uuid(), nullable=False, comment="The entry marked merged into it."),
        sa.Column(
            "claim_id",
            sa.Uuid(),
            nullable=False,
            comment="A claim behind the merged entry, now also a source of the kept one.",
        ),
        sa.Column(
            "learned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the two entries were found to be one event.",
        ),
        sa.CheckConstraint(
            "catalyst_id <> merged_id", name=op.f("ck_catalyst_merged_source_not_itself_d17026b6")
        ),
        sa.ForeignKeyConstraint(
            ["catalyst_id"],
            ["catalyst.id"],
            name=op.f("fk_catalyst_merged_source_catalyst_id_catalyst"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["merged_id"],
            ["catalyst.id"],
            name=op.f("fk_catalyst_merged_source_merged_id_catalyst"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"],
            ["claim.id"],
            name=op.f("fk_catalyst_merged_source_claim_id_claim"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalyst_merged_source")),
        sa.UniqueConstraint(
            "catalyst_id", "claim_id", name=op.f("uq_catalyst_merged_source_catalyst_id_claim_id")
        ),
        comment=(
            "A claim behind an entry merged into another as the same event, shown as its extra source "
            "(ADR 0081)."
        ),
    )
    op.execute(f'ALTER TABLE "{TABLE}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration, refusing while any entry is merged."""
    used = op.get_bind().execute(
        sa.text(
            "SELECT (SELECT count(*) FROM catalyst WHERE status = 'merged') "
            "+ (SELECT count(*) FROM catalyst_merged_source)"
        )
    )
    if used.scalar_one():
        raise RuntimeError("entries are merged (ADR 0081); downgrading would show both entries again")
    op.drop_table(TABLE)
    _status(NEW_STATUS, OLD_STATUS, OLD_COMMENT, NEW_COMMENT)
