"""The indication job: both readers on published catalysts without an indication (ADR 0053, decision 2).

The models are faked; the tests check what plain code keeps. A found indication is a claim whose
quote is its sentence; every try is kept, so a re-run asks nothing again; no catalyst is written.
"""

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.indicate import CONFIRMED_CHECK, REDRAWN, claim_field, indicate_catalysts
from catalyst.extract.text import document_text, find_quote
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.mesh import MeshIndex
from catalyst.publish.gate import match_quote
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimCheck,
    ClaimSubject,
    Company,
    DateConfidence,
    DatePrecision,
    Drug,
    DrugRole,
    Entity,
    IndicationAttempt,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore

FIXTURE = Path(__file__).parents[1] / "fixtures" / "mesh" / "desc2026_excerpt.xml.gz"
EVENT = "Topline data from the Phase 3 RAINIER trial of zentocel are expected in the second half of 2027."
TARGET = "Zentocel is in a Phase 3 trial in metastatic melanoma."
RELEASE = f"<html><body><p>Acme Bio news</p><p>{EVENT}</p><p>{TARGET}</p></body></html>".encode()
NOW = datetime(2026, 11, 12, 15, tzinfo=UTC)


@cache
def _index() -> MeshIndex:
    return MeshIndex.from_xml(FIXTURE.read_bytes())


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def seed_catalyst(
    factory: sessionmaker[Session], store: LocalSnapshotStore, n: int = 1, role: DrugRole = DrugRole.ALONE
) -> UUID:
    """Publish one catalyst the way the writer does, from a claim naming the drug as the release does."""
    fetched = FetchedDocument(
        source_key=f"sec:{n}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{n}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 10, 1, 20, tzinfo=UTC),
        content=RELEASE,
        content_type="text/html",
    )
    store_fetched(fetched, store, factory)
    span = find_quote(document_text(RELEASE, "text/html"), EVENT)
    assert span is not None
    with factory.begin() as session:
        document = session.execute(
            select(TABLES[SourceDocument].c.id).where(TABLES[SourceDocument].c.source_key == f"sec:{n}")
        ).scalar_one()
        company = Company(cik=1000 + n, name=f"Acme Bio {n}, Inc.")
        drug = Drug(company_id=company.id, preferred_name="zentocel")
        claim = Claim(
            source_document_id=document,
            subject_type=ClaimSubject.CATALYST,
            field="catalyst:1",
            value={"catalyst_type": "topline_readout", "drug": "zentocel"},
            quote=span.quote,
            quote_start=span.start,
            quote_end=span.end,
            agent="extractor",
            prompt_id="extract_catalysts",
            prompt_version="1.0.0",
            provider="anthropic",
            model="claude-sonnet-5-5",
            extracted_at=NOW,
        )
        catalyst = Catalyst(
            catalyst_type=CatalystType.TOPLINE_READOUT,
            company_id=company.id,
            title="zentocel: topline data (RAINIER)",
            window_start=date(2027, 7, 1),
            window_end=date(2027, 12, 31),
            window_precision=DatePrecision.HALF,
            window_raw_text="second half of 2027",
            date_confidence=DateConfidence.COMPANY_CONFIRMED,
            status=CatalystStatus.UPCOMING,
        )
        for record in (company, drug, claim, catalyst):
            _add(session, record)
        values = catalyst.model_dump(mode="json")
        values["drugs"] = [{"drug_id": str(drug.id), "role": role.value}]
        for name in CatalystField:
            if values.get(name.value) is not None:
                _add(
                    session,
                    CatalystChange(
                        catalyst_id=catalyst.id,
                        field=name,
                        new_value=values[name.value],
                        claim_id=claim.id,
                        learned_at=NOW,
                    ),
                )
        _add(session, CatalystDrug(catalyst_id=catalyst.id, drug_id=drug.id, role=role, claim_id=claim.id))
    return catalyst.id


@dataclass
class FakeReaders:
    """The first reader picks the sentence naming `disease`; the second answers as the test sets."""

    disease: str | None = "melanoma"
    states: bool = True
    reason: str = "states_target"
    sent: list[str] = field(default_factory=list[str])
    name: str = "fake"

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.sent.append(prompt.id)
        if prompt.id == "indicate_catalysts":
            lines = re.findall(r"^\[(\d+)\] (.*)$", document, flags=re.MULTILINE)
            number = next((int(n) for n, text in lines if self.disease and self.disease in text), None)
            answer: dict[str, Any] = {"sentence": number, "disease": self.disease if number else None}
        else:
            answer = {"states": self.states, "reason": self.reason}
        return Completion(json.dumps(answer), model, "req", "end_turn", TokenUsage(input=100, output=20))


def _client(fake: FakeReaders, cap: str = "5") -> ModelClient:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    role = RoleConfig(provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(cap))
    config = ModelConfig(
        version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role, "verifier": role}
    )
    return ModelClient(config, {"fake": fake}, InMemoryLedger(), now=lambda: NOW)


def run_readers(
    factory: sessionmaker[Session], store: LocalSnapshotStore, fake: FakeReaders, cap: str = "5"
) -> dict[str, Any]:
    report = indicate_catalysts(
        factory, store, _client(fake, cap), limit=10, index=_index(), now=lambda: NOW, gate=lambda: []
    )
    return report.summary()


def all_rows(factory: sessionmaker[Session], model: type[Entity]) -> list[Any]:
    with factory() as session:
        return list(session.execute(select(TABLES[model])).all())


def test_a_confirmed_indication_is_kept_as_a_claim_whose_quote_is_its_sentence(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    summary = run_readers(session_factory, store, FakeReaders())
    assert (summary["considered"], summary["found"], summary["refused"]) == (1, 1, {})
    (claim,) = [c for c in all_rows(session_factory, Claim) if c.field == claim_field(catalyst_id)]
    assert claim.quote == TARGET
    assert claim.subject_id == catalyst_id
    assert claim.value["words"] == "melanoma"
    assert claim.value["mesh_name"] == "Melanoma"
    assert document_text(RELEASE, "text/html")[claim.quote_start : claim.quote_end] == TARGET
    (check,) = [c for c in all_rows(session_factory, ClaimCheck) if c.claim_id == claim.id]
    assert (check.check_name, check.outcome) == (CONFIRMED_CHECK, "pass")
    (attempt,) = all_rows(session_factory, IndicationAttempt)
    assert (attempt.outcome, attempt.indication_claim_id) == ("found", claim.id)


def test_the_job_never_writes_a_catalyst(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    changes = len(all_rows(session_factory, CatalystChange))
    run_readers(session_factory, store, FakeReaders())
    (row,) = [c for c in all_rows(session_factory, Catalyst) if c.id == catalyst_id]
    assert row.indication_id is None
    assert len(all_rows(session_factory, CatalystChange)) == changes


def test_a_re_run_asks_nothing_again(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    again = FakeReaders()
    summary = run_readers(session_factory, store, again)
    assert again.sent == []
    assert (summary["considered"], summary["already_tried"]) == (0, 1)


def test_a_refusal_is_kept_with_its_reason_and_no_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    summary = run_readers(session_factory, store, FakeReaders(states=False, reason="other_drug"))
    assert summary["found"] == 0
    assert summary["refused"] == {"the second reader did not confirm the sentence states the disease": 1}
    (attempt,) = all_rows(session_factory, IndicationAttempt)
    assert (attempt.outcome, attempt.indication_claim_id) == ("refused", None)
    assert attempt.reason == "the second reader did not confirm the sentence states the disease"
    assert not [c for c in all_rows(session_factory, Claim) if c.field == claim_field(catalyst_id)]


def test_no_model_is_called_while_the_gate_finds_a_problem(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    fake = FakeReaders()
    report = indicate_catalysts(
        session_factory,
        store,
        _client(fake),
        limit=10,
        index=_index(),
        now=lambda: NOW,
        gate=lambda: ["indicate_catalysts has no scores.json"],
    )
    assert report.blocked == ["indicate_catalysts has no scores.json"]
    assert fake.sent == []
    assert all_rows(session_factory, IndicationAttempt) == []


def test_without_a_stored_mesh_file_nothing_is_asked(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    fake = FakeReaders()
    report = indicate_catalysts(
        session_factory, store, _client(fake), limit=10, now=lambda: NOW, gate=lambda: []
    )
    assert report.blocked
    assert "MeSH" in report.blocked[0]
    assert fake.sent == []


def test_a_drug_tested_in_combination_is_left_alone(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store, role=DrugRole.IN_COMBINATION)
    fake = FakeReaders()
    summary = run_readers(session_factory, store, fake)
    assert (summary["considered"], fake.sent) == (0, [])


def test_a_role_at_its_cap_stops_the_job(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    store = LocalSnapshotStore(tmp_path)
    seed_catalyst(session_factory, store)
    summary = run_readers(session_factory, store, FakeReaders(), cap="0.000001")
    assert summary["stopped"]
    assert summary["considered"] == 0
    assert all_rows(session_factory, IndicationAttempt) == []


def test_a_kept_indication_passes_the_publish_gates_own_quote_check(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    run_readers(session_factory, store, FakeReaders())
    (row,) = [c for c in all_rows(session_factory, Claim) if c.field == claim_field(catalyst_id)]
    claim = Claim.model_validate(row._asdict())
    result = match_quote(claim, document_text(RELEASE, "text/html"))
    assert result.outcome == "pass", result.reason


def test_a_later_try_that_names_another_disease_for_the_same_sentence_is_refused(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst_id = seed_catalyst(session_factory, store)
    span = find_quote(document_text(RELEASE, "text/html"), TARGET)
    assert span is not None
    with session_factory.begin() as session:  # an earlier run kept this sentence with another disease
        document = session.execute(select(TABLES[SourceDocument].c.id)).scalar_one()
        earlier = Claim(
            source_document_id=document,
            subject_type=ClaimSubject.CATALYST,
            subject_id=catalyst_id,
            field=claim_field(catalyst_id),
            value={"mesh_id": "D000001", "mesh_name": "Other", "words": "other", "text_version": "1"},
            quote=span.quote,
            quote_start=span.start,
            quote_end=span.end,
            agent="extractor",
            prompt_id="indicate_catalysts",
            prompt_version="1.0.0",
            provider="fake",
            model="claude-sonnet-5-5",
            extracted_at=NOW,
        )
        _add(session, earlier)
    summary = run_readers(session_factory, store, FakeReaders())
    assert summary["found"] == 0
    (attempt,) = all_rows(session_factory, IndicationAttempt)
    assert (attempt.outcome, attempt.reason, attempt.indication_claim_id) == ("refused", REDRAWN, None)
    assert not [c for c in all_rows(session_factory, ClaimCheck) if c.claim_id == earlier.id]
