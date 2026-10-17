"""The second-reading job on a disposable database (ADR 0018, part 2).

The verifier reads each release the extractor read, once per prompt version and model, and
plain code stores whether each extracted catalyst agrees with the second reading.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.repository import link_company_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.extract.run import extract_documents
from catalyst.llm.client import Completion, ModelClient, ModelError
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.schemas import (
    Claim,
    ClaimCheck,
    DocumentMatch,
    DocumentReading,
    Drug,
    DrugName,
    DrugNameKind,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify.second_run import run_second_reading

SENTENCE = "Topline data from the Phase 3 RAINIER trial of zentocel are expected in the second half of 2027."
RELEASE = f"<html><body><p>Acme Bio news</p><p>{SENTENCE}</p></body></html>".encode()
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def _event(**changes: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "catalyst_type": "topline_readout",
        "title": "Phase 3 RAINIER topline data",
        "drug": "zentocel",
        "indication": None,
        "date_text": "second half of 2027",
        "quote": SENTENCE,
    }
    return event | changes


def _config(cap: str = "5") -> ModelConfig:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    role = RoleConfig(provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(5))
    verifier = role.model_copy(update={"daily_cap_usd": Decimal(cap)})
    return ModelConfig(
        version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role, "verifier": verifier}
    )


@dataclass
class _Fake:
    """Answers the extractor with one event and the verifier with whatever the test sets."""

    verifier: list[dict[str, Any]] = field(default_factory=lambda: [_event()])
    name: str = "fake"
    calls: list[str] = field(default_factory=list[str])
    fail: bool = False

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.calls.append(prompt.id)
        if prompt.id == "verify_catalysts":
            if self.fail:
                raise ModelError("the provider is down")
            events = self.verifier
        else:
            events = [_event()]
        return Completion(
            json.dumps({"catalysts": events}), model, "req", "end_turn", TokenUsage(input=500, output=100)
        )


def _release(factory: sessionmaker[Session], store: LocalSnapshotStore, key: str = "a") -> None:
    fetched = FetchedDocument(
        source_key=f"sec:{key}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 10, 14, 20, tzinfo=UTC),
        content=RELEASE.replace(b"Acme", key.encode()),
        content_type="text/html",
    )
    store_fetched(fetched, store, factory)


def _extracted(
    factory: sessionmaker[Session], tmp_path: Path, fake: _Fake, config: ModelConfig | None = None
) -> tuple[LocalSnapshotStore, ModelClient]:
    store = LocalSnapshotStore(tmp_path)
    _release(factory, store)
    client = ModelClient(config or _config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    extract_documents(factory, store, client, limit=10, now=lambda: NOW)
    return store, client


def _run(
    factory: sessionmaker[Session],
    store: LocalSnapshotStore,
    client: ModelClient,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    report = run_second_reading(
        factory, store, client, model="claude-sonnet-5-5", limit=10, now=lambda: NOW, problems=problems or []
    )
    return report.summary()


def _checks(factory: sessionmaker[Session]) -> list[tuple[str, str | None]]:
    check = TABLES[ClaimCheck]
    with factory() as session:
        rows = session.execute(
            select(check.c.outcome, check.c.reason).where(check.c.check_name == "second_reading_agrees")
        )
        return [(r.outcome, r.reason) for r in rows]


def _count(factory: sessionmaker[Session], model: type[Any], **where: object) -> int:
    table = TABLES[model]
    query = select(func.count()).select_from(table).where(*(table.c[k] == v for k, v in where.items()))
    with factory() as session:
        return session.execute(query).scalar_one()


def test_an_unscored_second_reading_does_not_run(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake()
    store, client = _extracted(session_factory, tmp_path, fake)
    summary = _run(session_factory, store, client, problems=["verify_catalysts has no scores.json"])
    assert fake.calls == ["extract_catalysts"]
    assert summary["skipped"] == ["verify_catalysts has no scores.json"]
    assert _count(session_factory, DocumentReading) == 0
    assert _checks(session_factory) == []


def test_an_agreeing_second_reading_passes_the_extracted_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake()
    store, client = _extracted(session_factory, tmp_path, fake)
    summary = _run(session_factory, store, client)
    assert fake.calls == ["extract_catalysts", "verify_catalysts"]
    assert summary == {
        "skipped": [],
        "documents_read": 1,
        "documents_failed": 0,
        "claims_compared": 1,
        "claims_agreeing": 1,
        "stopped_early": None,
    }
    assert _checks(session_factory) == [("pass", None)]
    assert _count(session_factory, Claim, agent="verifier") == 1
    assert _count(session_factory, DocumentReading, agent="verifier", catalysts=1) == 1


def test_a_re_run_reads_nothing_again_and_keeps_one_row_per_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake()
    store, client = _extracted(session_factory, tmp_path, fake)
    _run(session_factory, store, client)
    summary = _run(session_factory, store, client)
    assert fake.calls == ["extract_catalysts", "verify_catalysts"]
    assert (summary["documents_read"], summary["claims_compared"]) == (0, 1)
    assert _checks(session_factory) == [("pass", None)]


def test_a_second_reading_that_finds_nothing_is_kept_and_fails_the_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake(verifier=[])
    store, client = _extracted(session_factory, tmp_path, fake)
    _run(session_factory, store, client)
    _run(session_factory, store, client)
    assert fake.calls.count("verify_catalysts") == 1
    assert _count(session_factory, DocumentReading, agent="verifier", catalysts=0) == 1
    assert _checks(session_factory) == [("fail", "the second reading did not find this event")]


def test_an_event_in_another_sentence_with_another_window_is_not_the_same_event(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    other = "Topline data from the Phase 3 RAINIER trial of zentocel are expected in 2028."
    fake = _Fake(verifier=[_event(date_text="2028", quote=other)])
    store = LocalSnapshotStore(tmp_path)
    content = RELEASE.replace(b"</body>", f"<p>{other}</p></body>".encode())
    fetched = FetchedDocument(
        source_key="sec:a",
        url="https://www.sec.gov/Archives/edgar/data/1/a.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=datetime(2026, 10, 14, 20, tzinfo=UTC),
        content=content,
        content_type="text/html",
    )
    store_fetched(fetched, store, session_factory)
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    _run(session_factory, store, client)
    # Different sentences, different windows, the same drug: not the same event.
    assert _checks(session_factory) == [("fail", "the second reading did not find this event")]


def test_a_second_reading_proposal_with_a_problem_does_not_vouch(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake(verifier=[_event(date_text="first half of 2027")])  # date words not in the quote
    store, client = _extracted(session_factory, tmp_path, fake)
    _run(session_factory, store, client)
    assert _checks(session_factory) == [("fail", "the second reading did not find this event")]


def test_drugs_the_reference_tables_call_the_same_agree(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake(verifier=[_event(drug="ZEN-101")])
    store = LocalSnapshotStore(tmp_path)
    _release(session_factory, store)
    with session_factory.begin() as session:
        company = upsert_company(session, 1, "Acme Bio", "2834").id
        document = session.execute(select(TABLES[SourceDocument].c.id)).scalar_one()
        link_company_document(session, company, document, DocumentMatch.SEC_CIK)
        drug = Drug(company_id=company, preferred_name="zentocel")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        for name, kind in (("zentocel", DrugNameKind.INN), ("ZEN-101", DrugNameKind.CODE)):
            row = DrugName(drug_id=drug.id, name=name, kind=kind)
            session.execute(insert(TABLES[DrugName]).values(**row.model_dump()))
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    _run(session_factory, store, client)
    assert _checks(session_factory) == [("pass", None)]


def test_the_verifier_at_its_cap_stops_and_says_so(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake()
    store, client = _extracted(session_factory, tmp_path, fake, _config(cap="0.0001"))
    summary = _run(session_factory, store, client)
    assert fake.calls == ["extract_catalysts"]
    assert summary["stopped_early"] is not None
    assert _count(session_factory, DocumentReading) == 0


def test_releases_with_no_extracted_claims_are_not_read(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake()
    store = LocalSnapshotStore(tmp_path)
    _release(session_factory, store)
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    assert _run(session_factory, store, client)["documents_read"] == 0
    assert fake.calls == []


def test_a_failed_model_call_leaves_the_release_unread_for_the_next_run(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake(fail=True)
    store, client = _extracted(session_factory, tmp_path, fake)
    summary = _run(session_factory, store, client)
    assert (summary["documents_read"], summary["documents_failed"]) == (0, 1)
    assert _count(session_factory, DocumentReading) == 0
    assert _checks(session_factory) == []  # no row: the publish gate counts that as not passing
    fake.fail = False
    assert _run(session_factory, store, client)["documents_read"] == 1


def test_an_older_reading_of_the_same_prompt_never_vouches(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    fake = _Fake()
    store, client = _extracted(session_factory, tmp_path, fake)
    _run(session_factory, store, client)  # read by claude-sonnet-5-5: agrees
    fake.verifier = []
    report = run_second_reading(
        session_factory,
        store,
        client,
        model="claude-opus-5-5",
        limit=10,
        now=lambda: datetime(2026, 10, 18, 12, tzinfo=UTC),
        problems=[],
    )
    assert report.documents_read == 1
    # The new model's reading found nothing; the old model's agreeing claim does not count.
    assert _checks(session_factory) == [("fail", "the second reading did not find this event")]
