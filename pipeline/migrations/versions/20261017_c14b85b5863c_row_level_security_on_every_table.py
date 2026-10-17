"""Turn on row-level security for every table, with no policies (ADR 0011).

Supabase serves tables in the public schema to anyone holding the project's publishable key
unless row-level security is on. With it on and no policies, only the table owner and
Supabase's secret-key role can read or write; the pipeline connects as one of those.
New tables must turn it on in their own migration; tests/test_tables.py checks every table.

Revision ID: c14b85b5863c
Revises: e99fd5eb7183
Create Date: 2026-10-17 16:12:26.675056
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c14b85b5863c"
down_revision: str | None = "e99fd5eb7183"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "catalyst",
    "catalyst_change",
    "catalyst_trial",
    "claim",
    "company",
    "company_document",
    "company_name",
    "company_ticker",
    "drug",
    "drug_name",
    "drug_target",
    "indication",
    "model_call",
    "outcome",
    "source_document",
    "target",
    "trial",
)


def upgrade() -> None:
    """Apply this migration."""
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    for table in TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
