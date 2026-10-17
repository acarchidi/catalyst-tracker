"""Several drugs per catalyst, each tested alone or in combination (ADR 0016).

Adds `catalyst_drug` and `drug_component`, both claim-backed, and drops `catalyst.drug_id`.
Change history names the drug list `drugs` instead of `drug_id`. Each step refuses to run when
it would lose stored data, so upgrade and downgrade never drop a link silently.

Revision ID: 6b3ab8320926
Revises: c14b85b5863c
Create Date: 2026-10-17 21:22:58.354474
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

OLD_FIELD_CHECK = "ck_catalyst_change_field_allowed_9717b233"
NEW_FIELD_CHECK = "ck_catalyst_change_field_allowed_f350a648"
FIELDS = (
    "'catalyst_type', 'company_id', '{drugs}', 'indication_id', 'title', 'window_start', "
    "'window_end', 'window_precision', 'window_raw_text', 'date_confidence', 'status'"
)
NEW_TABLES = ("catalyst_drug", "drug_component")


def _refuse_if(sql: str, message: str) -> None:
    if op.get_bind().execute(sa.text(sql)).scalar_one():
        raise RuntimeError(message)


revision: str = "6b3ab8320926"
down_revision: str | None = "c14b85b5863c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this migration."""
    _refuse_if(
        "SELECT count(*) FROM catalyst WHERE drug_id IS NOT NULL",
        "catalysts still name a drug in catalyst.drug_id; move them to catalyst_drug with their claims first",
    )
    _refuse_if(
        "SELECT count(*) FROM catalyst_change WHERE field = 'drug_id'",
        "change history has 'drug_id' rows, which is append-only and cannot be renamed",
    )
    op.create_table(
        "catalyst_drug",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("catalyst_id", sa.Uuid(), nullable=False, comment="The catalyst."),
        sa.Column("drug_id", sa.Uuid(), nullable=False, comment="A drug the catalyst concerns."),
        sa.Column(
            "role",
            sa.String(length=14),
            nullable=False,
            comment=(
                "Whether the drug is tested alone or in combination; a combination product itself "
                "(for example SPY120) is linked as in_combination."
            ),
        ),
        sa.Column("claim_id", sa.Uuid(), nullable=False, comment="The claim whose quote states this link."),
        sa.CheckConstraint(
            "role IN ('alone', 'in_combination')", name=op.f("ck_catalyst_drug_role_allowed_48ebd8dc")
        ),
        sa.ForeignKeyConstraint(
            ["catalyst_id"],
            ["catalyst.id"],
            name=op.f("fk_catalyst_drug_catalyst_id_catalyst"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name=op.f("fk_catalyst_drug_claim_id_claim"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["drug_id"], ["drug.id"], name=op.f("fk_catalyst_drug_drug_id_drug"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_catalyst_drug")),
        sa.UniqueConstraint(
            "catalyst_id", "drug_id", "role", name=op.f("uq_catalyst_drug_catalyst_id_drug_id_role")
        ),
        comment="Links a catalyst to one drug it concerns, tested alone or in combination (ADR 0016).",
    )
    op.create_table(
        "drug_component",
        sa.Column("id", sa.Uuid(), nullable=False, comment="Internal identifier."),
        sa.Column("combination_id", sa.Uuid(), nullable=False, comment="The combination."),
        sa.Column("component_id", sa.Uuid(), nullable=False, comment="One drug in it."),
        sa.Column("claim_id", sa.Uuid(), nullable=False, comment="The claim whose quote names this part."),
        sa.CheckConstraint(
            "combination_id <> component_id", name=op.f("ck_drug_component_not_itself_117522bf")
        ),
        sa.ForeignKeyConstraint(
            ["claim_id"], ["claim.id"], name=op.f("fk_drug_component_claim_id_claim"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["combination_id"],
            ["drug.id"],
            name=op.f("fk_drug_component_combination_id_drug"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["component_id"],
            ["drug.id"],
            name=op.f("fk_drug_component_component_id_drug"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drug_component")),
        sa.UniqueConstraint(
            "combination_id", "component_id", name=op.f("uq_drug_component_combination_id_component_id")
        ),
        comment="One part of a combination drug, recorded only where a source states it (ADR 0016).",
    )
    op.drop_constraint(op.f("fk_catalyst_drug_id_drug"), "catalyst", type_="foreignkey")
    op.drop_column("catalyst", "drug_id")
    op.drop_constraint(op.f(OLD_FIELD_CHECK), "catalyst_change", type_="check")
    op.create_check_constraint(
        op.f(NEW_FIELD_CHECK), "catalyst_change", f"field IN ({FIELDS.format(drugs='drugs')})"
    )
    for table in NEW_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Reverse this migration."""
    _refuse_if(
        "SELECT count(*) FROM catalyst_drug", "catalyst_drug has links that catalyst.drug_id cannot hold"
    )
    _refuse_if("SELECT count(*) FROM drug_component", "drug_component has rows that would be lost")
    _refuse_if(
        "SELECT count(*) FROM catalyst_change WHERE field = 'drugs'",
        "change history has 'drugs' rows, which is append-only and cannot be renamed",
    )
    op.drop_constraint(op.f(NEW_FIELD_CHECK), "catalyst_change", type_="check")
    op.create_check_constraint(
        op.f(OLD_FIELD_CHECK), "catalyst_change", f"field IN ({FIELDS.format(drugs='drug_id')})"
    )
    op.add_column(
        "catalyst",
        sa.Column("drug_id", sa.UUID(), autoincrement=False, nullable=True, comment="The drug, if any."),
    )
    op.create_foreign_key(
        op.f("fk_catalyst_drug_id_drug"), "catalyst", "drug", ["drug_id"], ["id"], ondelete="RESTRICT"
    )
    op.drop_table("drug_component")
    op.drop_table("catalyst_drug")
