"""Sample checks: the weekly sample checker's verdict on each drawn catalyst (ADR 0041).

Adds `sample_check`, with row-level security on (ADR 0011). Each row is a stronger model's
answers on one published catalyst, turned into a verdict by plain code. The downgrade drops
the rows: they are a measurement, and the next weekly draw measures again.

Revision ID: 3798b407da9a
Revises: b3f1c7e2a9d4
Create Date: 2026-10-20 12:32:51.508156
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3798b407da9a"
down_revision: str | None = "b3f1c7e2a9d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "sample_check",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("catalyst_id", sa.Uuid(), nullable=False, comment="The catalyst checked."),
        sa.Column(
            "week_of",
            sa.Date(),
            nullable=False,
            comment="Monday (New York time) of the week whose random draw picked it.",
        ),
        sa.Column(
            "verdict",
            sa.String(length=9),
            nullable=False,
            comment="Agrees only when every checked point agrees.",
        ),
        sa.Column(
            "points",
            postgresql.JSONB(none_as_null=True, astext_type=sa.Text()),
            nullable=False,
            comment="Each checked point: its source claim and link, the published value, the answer, and why.",
        ),
        sa.Column("prompt_id", sa.String(length=100), nullable=False, comment="Prompt used."),
        sa.Column("prompt_version", sa.String(length=100), nullable=False, comment="Version of that prompt."),
        sa.Column("model", sa.String(length=100), nullable=False, comment="Model that answered."),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, comment="When the record was checked."
        ),
        sa.CheckConstraint(
            "jsonb_typeof(points) = 'array'", name=op.f("ck_sample_check_points_is_list_786e2994")
        ),
        sa.CheckConstraint(
            "verdict IN ('agrees', 'disagrees')", name=op.f("ck_sample_check_verdict_allowed_1d56cbd4")
        ),
        sa.ForeignKeyConstraint(
            ["catalyst_id"],
            ["catalyst.id"],
            name=op.f("fk_sample_check_catalyst_id_catalyst"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sample_check")),
        sa.UniqueConstraint(
            "catalyst_id",
            "week_of",
            "prompt_id",
            "prompt_version",
            "model",
            name=op.f("uq_sample_check_catalyst_id_week_of_prompt_id_prompt_version_model"),
        ),
        comment="The weekly sample checker's verdict on one published catalyst (spec: Weekly random sample; ADR 0041).",
    )
    op.execute('ALTER TABLE "sample_check" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_table("sample_check")
