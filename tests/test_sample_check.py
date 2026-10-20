"""The weekly sample checker (spec: Human review and accuracy measurement; ADR 0040).

Each week a repeatable random draw of published catalysts is checked against their sources by a
stronger model. The model only answers in JSON; plain code decides each point and writes the
verdict, and a record agrees only when every point it checked is supported by a quote that is
really in the source.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.read import published, read_session
from catalyst.audit.sample import (
    PROMPT,
    ROLE,
    checker_input,
    draw,
    judge,
    run_sample_check,
    week_of,
)
from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.text import document_text, find_quote
from catalyst.llm.client import Completion, ModelClient, ModelError
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt, load_prompt
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimSubject,
    Company,
    DateConfidence,
    DatePrecision,
    Drug,
    DrugRole,
    Entity,
    SampleCheck,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore

SENTENCE = "Topline data from the Phase 3 RAINIER trial of zentocel are expected in the second half of 2027."
RELEASE = f"<html><body><p>Acme Bio news</p><p>{SENTENCE}</p><p>About Acme Bio.</p></body></html>".encode()
PUBLISHED = datetime(2026, 10, 1, 20, tzinfo=UTC)
WEEK = date(2026, 10, 19)  # a Monday
NOW = datetime(2026, 10, 20, 15, tzinfo=UTC)
POINTS = {"type": "topline readout", "date": "2027-07-01 to 2027-12-31"}


def _answer(point: str, answer: str = "supported", quote: str = SENTENCE) -> dict[str, str]:
    return {"point": point, "answer": answer, "quote": quote, "reason": "The release says so."}


# --- Plain-code rules -------------------------------------------------------------------------


def test_week_of_is_the_new_york_monday() -> None:
    assert week_of(datetime(2026, 10, 25, 3, tzinfo=UTC)) == WEEK  # still Saturday night in New York
    assert week_of(datetime(2026, 10, 26, 5, tzinfo=UTC)) == date(2026, 10, 26)


def test_a_point_agrees_only_when_supported_with_a_quote_from_the_source() -> None:
    verdicts = judge(POINTS, [_answer("type"), _answer("date")], f"News. {SENTENCE} More.")
    assert [v["agrees"] for v in verdicts] == [True, True]
    assert [v["point"] for v in verdicts] == ["type", "date"]


def test_a_supported_answer_whose_quote_is_not_in_the_source_disagrees() -> None:
    verdicts = judge(POINTS, [_answer("type"), _answer("date", quote="Data in 2026.")], SENTENCE)
    assert verdicts[1]["agrees"] is False
    assert verdicts[1]["reason"] == "the checker's quote is not in the source"


def test_contradicted_not_stated_and_unanswered_points_disagree() -> None:
    answers = [_answer("type", "contradicted")]
    verdicts = {v["point"]: v for v in judge(POINTS, answers, SENTENCE)}
    assert verdicts["type"]["reason"] == "the checker says the source states something else"
    assert verdicts["date"]["reason"] == "the checker gave no answer for this point"
    stated = judge({"type": "x"}, [_answer("type", "not_stated", "")], SENTENCE)
    assert stated[0]["reason"] == "the checker says the source does not state it"
    assert not any(v["agrees"] for v in [*verdicts.values(), *stated])


def test_two_answers_for_one_point_count_only_if_both_agree() -> None:
    answers = [_answer("type"), _answer("type", "contradicted"), _answer("date")]
    verdicts = {v["point"]: v for v in judge(POINTS, answers, SENTENCE)}
    assert verdicts["type"]["agrees"] is False
    assert verdicts["date"]["agrees"] is True


def test_answers_for_points_not_asked_are_ignored() -> None:
    verdicts = judge({"date": "x"}, [_answer("date"), _answer("drugs", "contradicted")], SENTENCE)
    assert [(v["point"], v["agrees"]) for v in verdicts] == [("date", True)]


def test_the_model_reads_the_date_the_points_and_the_document() -> None:
    shown = checker_input(POINTS, date(2026, 10, 1), "The text.")
    assert shown.startswith("Publication date: 2026-10-01\n")
    assert "1. type: topline readout\n2. date: 2027-07-01 to 2027-12-31" in shown
    assert shown.endswith("Document:\n\nThe text.")


def test_the_prompt_answers_only_in_json_with_one_answer_per_point() -> None:
    prompt = load_prompt(*PROMPT)
    item = prompt.output_schema["properties"]["points"]["items"]
    assert item["properties"]["answer"]["enum"] == ["supported", "contradicted", "not_stated"]
    assert ROLE == "analyst"  # the Opus role: stronger than the Sonnet readers it checks


# --- The job on a disposable database ------------------------------------------------------------


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def _seed(factory: sessionmaker[Session], store: LocalSnapshotStore, n: int, learned: datetime) -> UUID:
    """Publish one catalyst the way the writer does: every value backed by a change and its claim."""
    fetched = FetchedDocument(
        source_key=f"sec:{n}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{n}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type="8-K EX-99.1",
        published_at=PUBLISHED,
        content=RELEASE,
        content_type="text/html",
    )
    store_fetched(fetched, store, factory)
    span = find_quote(document_text(RELEASE, "text/html"), SENTENCE)
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
            value={"catalyst_type": "topline_readout"},
            quote=span.quote,
            quote_start=span.start,
            quote_end=span.end,
            agent="extractor",
            prompt_id="extract_catalysts",
            prompt_version="1.0.0",
            provider="anthropic",
            model="claude-sonnet-5-5",
            extracted_at=learned,
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
        values["drugs"] = [{"drug_id": str(drug.id), "role": "alone"}]
        for name in CatalystField:
            if values.get(name.value) is not None:
                change = CatalystChange(
                    catalyst_id=catalyst.id,
                    field=name,
                    new_value=values[name.value],
                    claim_id=claim.id,
                    learned_at=learned,
                )
                _add(session, change)
        _add(
            session,
            CatalystDrug(catalyst_id=catalyst.id, drug_id=drug.id, role=DrugRole.ALONE, claim_id=claim.id),
        )
    return catalyst.id


def _config(cap: str = "5") -> ModelConfig:
    price = Price(input=Decimal(4), cache_write=Decimal(5), cache_read=Decimal(0), output=Decimal(20))
    role = RoleConfig(provider="fake", model="claude-opus-5-5", max_tokens=1000, daily_cap_usd=Decimal(cap))
    return ModelConfig(version="test", pricing={"claude-opus-5-5": price}, roles={ROLE: role})


@dataclass
class _Fake:
    """Answers every point as the test sets, or supported with the release's own sentence."""

    answers: dict[str, str] = field(default_factory=dict[str, str])
    fail: bool = False
    name: str = "fake"
    documents: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.documents.append(document)
        if self.fail:
            raise ModelError("the provider is down")
        asked = [
            line.split(". ", 1)[1].split(":", 1)[0] for line in document.splitlines() if line[:1].isdigit()
        ]
        points = [_answer(point, self.answers.get(point, "supported")) for point in asked]
        text = json.dumps({"points": points})
        return Completion(text, model, "req", "end_turn", TokenUsage(input=2000, output=500))


def _run(
    factory: sessionmaker[Session],
    store: LocalSnapshotStore,
    fake: _Fake,
    *,
    limit: int = 10,
    cap: str = "5",
) -> dict[str, Any]:
    client = ModelClient(_config(cap), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = run_sample_check(factory, store, client, week=WEEK, limit=limit, now=lambda: NOW)
    return report.summary()


def _rows(factory: sessionmaker[Session]) -> list[Any]:
    with factory() as session:
        return list(session.execute(select(TABLES[SampleCheck])).all())


def test_a_record_whose_source_states_every_point_agrees(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst = _seed(session_factory, store, 1, PUBLISHED)
    fake = _Fake()
    summary = _run(session_factory, store, fake)
    assert summary | {"disagreements": []} == {
        "week_of": "2026-10-19",
        "drawn": 1,
        "already_checked": 0,
        "checked": 1,
        "agrees": 1,
        "disagrees": 0,
        "documents_failed": 0,
        "stopped_early": None,
        "left_this_week": 0,
        "disagreements": [],
    }
    (row,) = _rows(session_factory)
    assert (row.catalyst_id, row.verdict, row.week_of, row.model) == (
        catalyst,
        "agrees",
        WEEK,
        "claude-opus-5-5",
    )
    assert sorted(p["point"] for p in row.points) == ["company", "date", "drugs", "type"]
    # The model saw the published values beside the visible text, and nothing else.
    (shown,) = fake.documents
    assert "date: 2027-07-01 to 2027-12-31 (half), written in the source as 'second half of 2027'" in shown
    assert "drugs: zentocel (alone)" in shown
    assert shown.endswith(f"Acme Bio news\n{SENTENCE}\nAbout Acme Bio.")


def test_a_re_run_in_the_same_week_spends_nothing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _seed(session_factory, store, 1, PUBLISHED)
    _run(session_factory, store, _Fake())
    again = _Fake()
    summary = _run(session_factory, store, again)
    assert (summary["already_checked"], summary["checked"], again.documents) == (1, 0, [])
    assert len(_rows(session_factory)) == 1


def test_a_disagreement_is_stored_and_listed_with_code_words_only(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    catalyst = _seed(session_factory, store, 1, PUBLISHED)
    summary = _run(session_factory, store, _Fake(answers={"date": "contradicted"}))
    assert (summary["agrees"], summary["disagrees"]) == (0, 1)
    assert summary["disagreements"] == [
        {
            "catalyst_id": str(catalyst),
            "title": "zentocel: topline data (RAINIER)",
            "points": ["date"],
            "links": ["https://www.sec.gov/Archives/edgar/data/1/1.htm"],
        }
    ]
    (row,) = _rows(session_factory)
    assert row.verdict == "disagrees"
    (date_point,) = [p for p in row.points if p["point"] == "date"]
    assert date_point["checker_reason"] == "The release says so."  # kept for the owner, never in an alert


def test_the_draw_is_repeatable_and_only_from_records_published_before_the_week(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    old = [_seed(session_factory, store, n, PUBLISHED) for n in range(1, 6)]
    _seed(session_factory, store, 9, datetime(2026, 10, 19, 12, tzinfo=UTC))  # published this week
    with read_session(session_factory) as session:
        details = published(session)
    first = draw(details, WEEK, size=3)
    assert [d.id for d in first] == [d.id for d in draw(list(reversed(details)), WEEK, size=3)]
    assert len(first) == 3
    assert all(d.id in old for d in first)
    assert {d.id for d in draw(details, WEEK, size=30)} == set(old)
    assert all(d.id in old for d in draw(details, WEEK + timedelta(days=7), size=3))


def test_the_limit_and_the_cap_leave_the_rest_for_the_next_run(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    for n in range(1, 4):
        _seed(session_factory, store, n, PUBLISHED)
    summary = _run(session_factory, store, _Fake(), limit=1)
    assert (summary["checked"], summary["left_this_week"], summary["stopped_early"]) == (1, 2, None)
    capped = _run(session_factory, store, _Fake(), cap="0.01")
    assert capped["checked"] == 0
    assert capped["stopped_early"]
    assert capped["left_this_week"] == 2
    assert len(_rows(session_factory)) == 1


def test_a_failed_call_stores_nothing_and_is_counted(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _seed(session_factory, store, 1, PUBLISHED)
    summary = _run(session_factory, store, _Fake(fail=True))
    assert (summary["documents_failed"], summary["checked"], summary["left_this_week"]) == (1, 0, 1)
    with session_factory() as session:
        assert session.execute(select(func.count()).select_from(TABLES[SampleCheck])).scalar_one() == 0
