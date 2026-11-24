"""Company display names: the name readers see, the release that spells it, and its inputs (ADR 0068).

Adds three empty columns to `company`. The next run of the display-name step fills them; until
then the read layer shows the SEC name without its state suffix, so the upgrade needs no data
change. The downgrade drops them; the SEC names stay, and the code before this revision shows
them as it always did.

Revision ID: c3d5e7f9a1b2
Revises: 1b7e8f9c6561
Create Date: 2026-11-24 14:54:41.657168
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d5e7f9a1b2"
down_revision: str | None = "1b7e8f9c6561"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.add_column(
        "company",
        sa.Column(
            "display_name",
            sa.String(length=300),
            nullable=True,
            comment="Name shown to readers (ADR 0068): the company's own spelling in its own stored press releases, else the SEC name without its state suffix. Empty until first computed.",
        ),
    )
    op.add_column(
        "company",
        sa.Column(
            "display_name_document_id",
            sa.Uuid(),
            nullable=True,
            comment="The stored press release that spells the display name; empty when it is the SEC name.",
        ),
    )
    op.add_column(
        "company",
        sa.Column(
            "display_name_inputs",
            sa.String(length=64),
            nullable=True,
            comment="SHA-256 of what the display name was computed from: the code, the SEC name, and the releases read (ADR 0068); a run recomputes it only when this changes.",
        ),
    )
    op.create_foreign_key(
        op.f("fk_company_display_name_document_id_source_document"),
        "company",
        "source_document",
        ["display_name_document_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    """Revert this migration."""
    op.drop_constraint(
        op.f("fk_company_display_name_document_id_source_document"), "company", type_="foreignkey"
    )
    op.drop_column("company", "display_name_inputs")
    op.drop_column("company", "display_name_document_id")
    op.drop_column("company", "display_name")
