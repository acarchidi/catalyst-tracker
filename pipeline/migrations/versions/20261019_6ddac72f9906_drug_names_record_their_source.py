"""Drug names record their source; shared names are kept apart for review (ADR 0035).

Adds to `drug_name` where each name was first recorded (`source`: a claim's own document,
Drugs@FDA, or ClinicalTrials.gov) and the records that give it (`source_refs`). Names already
stored were all created by the matching job from a company's own document, so they become
`claim`, with no record listed. Adds `withheld_drug_name`, with row-level security on (ADR 0011),
for names several companies' records give. The drug list rebuilds those rows on every run, so the
downgrade may drop them; it also drops the two new columns, which the drug list can refill.

Revision ID: 6ddac72f9906
Revises: b3f1c7e2a9d4
Create Date: 2026-10-19 13:53:42.104480
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "6ddac72f9906"
down_revision: str | None = "b3f1c7e2a9d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_JSONB = postgresql.JSONB(none_as_null=True, astext_type=sa.Text())


def upgrade() -> None:
    """Apply this migration."""
    op.create_table(
        "withheld_drug_name",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column(
            "name", sa.String(length=300), nullable=False, comment="The name as one of its sources wrote it."
        ),
        sa.Column(
            "reason",
            sa.String(length=100),
            nullable=False,
            comment="Why it was not recorded, for example 'shared'.",
        ),
        sa.Column(
            "company_ids",
            _JSONB,
            nullable=False,
            comment="Covered companies whose records give the name, as a list; other sponsors are not listed.",
        ),
        sa.Column(
            "source_refs",
            _JSONB,
            nullable=False,
            comment="The records that give the name, as a list: FDA application or registry numbers.",
        ),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="When the drug list last found it shared.",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(company_ids) = 'array' AND jsonb_typeof(source_refs) = 'array'",
            name=op.f("ck_withheld_drug_name_lists_b36a34a5"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_withheld_drug_name")),
        sa.UniqueConstraint("name", name=op.f("uq_withheld_drug_name_name")),
        comment="A drug name the drug list did not record because several companies' records give it (ADR 0035).",
    )
    op.execute('ALTER TABLE "withheld_drug_name" ENABLE ROW LEVEL SECURITY')
    # Every stored name so far was recorded by the matching job from a claim's own document.
    op.add_column(
        "drug_name",
        sa.Column(
            "source",
            sa.String(length=7),
            nullable=False,
            server_default="claim",
            comment="Where the name was first found: a claim's document, Drugs@FDA, or ClinicalTrials.gov.",
        ),
    )
    op.add_column(
        "drug_name",
        sa.Column(
            "source_refs",
            _JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
            comment="The records that give this name, as a list: FDA application or registry numbers.",
        ),
    )
    op.alter_column("drug_name", "source", server_default=None)
    op.alter_column("drug_name", "source_refs", server_default=None)
    op.create_check_constraint(
        op.f("ck_drug_name_source_allowed_9163467f"), "drug_name", "source IN ('claim', 'openfda', 'ctgov')"
    )
    op.create_check_constraint(
        op.f("ck_drug_name_source_refs_is_list_7b78b8ed"), "drug_name", "jsonb_typeof(source_refs) = 'array'"
    )


def downgrade() -> None:
    """Reverse this migration."""
    op.drop_constraint(op.f("ck_drug_name_source_refs_is_list_7b78b8ed"), "drug_name", type_="check")
    op.drop_constraint(op.f("ck_drug_name_source_allowed_9163467f"), "drug_name", type_="check")
    op.drop_column("drug_name", "source_refs")
    op.drop_column("drug_name", "source")
    op.drop_table("withheld_drug_name")
