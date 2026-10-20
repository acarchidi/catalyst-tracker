"""The extraction job on a disposable database: claims stored once, spend caps respected."""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.run import MIN_WORDS, extract_documents
from catalyst.llm.client import Completion, ModelClient
from catalyst.llm.config import ModelConfig, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.schemas import Claim, DocumentReading, SourceTier
from catalyst.snapshots import LocalSnapshotStore

# A release has more than a headline: the job sends no copy with fewer than 50 readable words.
ABOUT = (
    b"<p>About Acme Bio: Acme Bio is a clinical-stage biotechnology company that develops medicines "
    b"for people with serious liver diseases. Its lead programme is in a Phase 3 trial, and two more "
    b"programmes are in earlier trials. The company is based in Boston, Massachusetts.</p>"
)
RELEASE = (
    b"<html><body><p>Acme Bio Reports Third Quarter 2026 Results</p>"
    b"<p>Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.</p>"
    + ABOUT
    + b"</body></html>"
)
ANSWER: dict[str, list[dict[str, str | None]]] = {
    "catalysts": [
        {
            "catalyst_type": "topline_readout",
            "title": "Phase 3 RAINIER topline data",
            "drug": None,
            "indication": None,
            "date_text": "second half of 2027",
            "quote": "Topline data from the Phase 3 RAINIER trial are expected in the second half of 2027.",
        }
    ]
}
NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)


def _config(cap: str = "5") -> ModelConfig:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    role = RoleConfig(provider="fake", model="claude-sonnet-5-5", max_tokens=1000, daily_cap_usd=Decimal(cap))
    return ModelConfig(version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role})


@dataclass
class _Fake:
    name: str = "fake"
    calls: list[str] = field(default_factory=list[str])
    answer: dict[str, list[dict[str, str | None]]] = field(default_factory=lambda: ANSWER)

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.calls.append(document)
        return Completion(
            json.dumps(self.answer), model, "req", "end_turn", TokenUsage(input=500, output=100)
        )


def _document(
    store: LocalSnapshotStore,
    factory: sessionmaker[Session],
    key: str,
    kind: str,
    content: bytes = RELEASE,
    content_type: str = "text/html",
) -> None:
    fetched = FetchedDocument(
        source_key=f"sec:{key}",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=kind,
        published_at=datetime(2026, 10, 14, 20, tzinfo=UTC),
        content=content.replace(b"Acme", key.encode()),
        content_type=content_type,
    )
    store_fetched(fetched, store, factory)


def _claims(factory: sessionmaker[Session]) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(TABLES[Claim])).scalar_one()


def test_press_releases_become_checked_claims_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    _document(store, session_factory, "b", "10-Q")
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    assert len(fake.calls) == 1  # the 10-Q is not a press release
    assert fake.calls[0].startswith("Publication date: 2026-10-14\n\n")
    assert report.summary() == {
        "documents_read": 1,
        "documents_failed": 0,
        "documents_skipped_already_done": 0,
        "documents_skipped_not_text": 0,
        "documents_skipped_no_readable_text": 0,
        "documents_skipped_too_large": 0,
        "documents_rechecked_still_skipped": 0,
        "catalysts_proposed": 1,
        "catalysts_passing_every_check": 1,
        "claims_stored": 1,
        "stopped_early": None,
    }
    assert report.documents[0].catalysts[0]["window_start"] == "2027-07-01"
    assert report.documents[0].catalysts[0]["problems"] == []
    again = extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    assert len(fake.calls) == 1
    assert again.summary()["documents_skipped_already_done"] == 1
    assert _claims(session_factory) == 1


def test_a_role_at_its_cap_stops_the_job_and_says_so(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    fake = _Fake()
    client = ModelClient(_config(cap="0.0001"), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10)
    assert fake.calls == []
    assert report.stopped is not None
    assert "daily cap" in report.stopped
    assert report.documents == []
    assert _claims(session_factory) == 0


def test_a_missing_copy_is_reported_and_the_job_goes_on(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    _document(store, session_factory, "b", "8-K EX-99.1")
    for copy in tmp_path.rglob("*"):
        if copy.is_file() and b"b Bio Reports" in copy.read_bytes():
            copy.unlink()
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10, since=date(2026, 10, 1))
    errors = [d.error for d in report.documents if d.error]
    assert len(errors) == 1
    assert errors[0].startswith("SnapshotNotFoundError")
    assert report.summary()["claims_stored"] == 1


@pytest.mark.parametrize("since", [date(2026, 10, 15)])
def test_documents_before_since_are_left_alone(
    session_factory: sessionmaker[Session], tmp_path: Path, since: date
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    assert extract_documents(session_factory, store, client, limit=10, since=since).documents == []


def test_two_events_in_one_sentence_are_both_kept(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Review of PR #16: claims sharing one quote must not collide on the claim table's unique key."""
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    shared = ANSWER["catalysts"][0]
    second = shared | {"catalyst_type": "regulatory_submission", "title": "NDA submission"}
    fake = _Fake(answer={"catalysts": [shared, second]})
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = extract_documents(session_factory, store, client, limit=10, now=lambda: NOW)
    assert report.summary()["claims_stored"] == 2
    assert _claims(session_factory) == 2


def _readings(factory: sessionmaker[Session]) -> list[tuple[str, str | None, int]]:
    table = TABLES[DocumentReading]
    query = select(table.c.prompt_version, table.c.skipped, table.c.catalysts).where(
        table.c.agent == "extractor"
    )
    with factory() as session:
        return [(r.prompt_version, r.skipped, r.catalysts) for r in session.execute(query)]


def _run(
    factory: sessionmaker[Session],
    store: LocalSnapshotStore,
    fake: _Fake,
    config: ModelConfig | None = None,
    limit: int = 10,
) -> dict[str, object]:
    client = ModelClient(config or _config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    return extract_documents(factory, store, client, limit=limit, now=lambda: NOW).summary()


def test_a_release_with_no_catalysts_is_read_once_per_prompt_version(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Run 39321573893 re-read 153 releases that had given no catalysts before, and spent the cap."""
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    fake = _Fake(answer={"catalysts": []})
    assert _run(session_factory, store, fake)["documents_read"] == 1
    assert _readings(session_factory) == [("1.0.0", None, 0)]
    again = _run(session_factory, store, fake)
    assert len(fake.calls) == 1
    assert (again["documents_read"], again["documents_skipped_already_done"]) == (0, 1)
    assert _readings(session_factory) == [("1.0.0", None, 0)]  # a re-run adds no second row
    # A reading made with an older prompt version does not count: a new prompt reads it again.
    with session_factory.begin() as session:
        session.execute(update(TABLES[DocumentReading]).values(prompt_version="0.9.0"))
    assert _run(session_factory, store, fake)["documents_read"] == 1
    assert len(fake.calls) == 2


def test_a_failed_read_is_not_recorded_and_runs_again(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    fake = _Fake(answer={"wrong": []})  # breaks the output schema, so the read did not complete
    assert _run(session_factory, store, fake)["documents_failed"] == 1
    assert _readings(session_factory) == []
    assert _run(session_factory, store, fake)["documents_failed"] == 1
    assert len(fake.calls) == 2


def test_a_pdf_copy_is_never_sent_to_the_model(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """A PDF exhibit read as raw bytes was 482,163 tokens and cost $0.96 in each of four runs."""
    store = LocalSnapshotStore(tmp_path)
    _document(
        store,
        session_factory,
        "a",
        "8-K EX-99.1",
        b"%PDF-1.7\n" + b"\x00\xff stream " * 500,
        "application/pdf",
    )
    fake = _Fake()
    summary = _run(session_factory, store, fake)
    assert fake.calls == []
    assert (summary["documents_read"], summary["documents_skipped_not_text"]) == (0, 1)
    assert _readings(session_factory) == [("1.0.0", "not_text", 0)]
    assert _run(session_factory, store, fake)["documents_skipped_already_done"] == 1


@pytest.mark.parametrize("content_type", ["text/html", "text/plain", "TEXT/HTML; charset=utf-8"])
def test_web_pages_and_plain_text_are_sent(
    session_factory: sessionmaker[Session], tmp_path: Path, content_type: str
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1", content_type=content_type)
    fake = _Fake()
    assert _run(session_factory, store, fake)["documents_read"] == 1
    assert len(fake.calls) == 1


def test_a_copy_with_no_readable_text_is_recorded_without_a_model_call(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """36 releases in run 39321573893 had no readable text, and each was still a paid call."""
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1", b"<html><body><img src='a.jpg'></body></html>")
    fake = _Fake()
    summary = _run(session_factory, store, fake)
    assert fake.calls == []
    assert (summary["documents_read"], summary["documents_skipped_no_readable_text"]) == (0, 1)
    assert _readings(session_factory) == [("1.0.0", "no_readable_text", 0)]
    # Not done: each run checks the copy again for free, and counts it once (Opus review of PR #79, B1).
    again = _run(session_factory, store, fake)
    assert fake.calls == []
    assert (again["documents_skipped_already_done"], again["documents_skipped_no_readable_text"]) == (0, 0)
    assert again["documents_rechecked_still_skipped"] == 1
    assert _readings(session_factory) == [("1.0.0", "no_readable_text", 0)]


def test_a_copy_with_no_readable_text_is_read_once_the_reader_finds_its_words(
    session_factory: sessionmaker[Session], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`visible_text` blanked four real releases on 2026-10-19; once it is fixed they must be read."""
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")

    def blank(_content: bytes, _type: str) -> str:
        return ""

    monkeypatch.setattr("catalyst.extract.run.visible_text", blank)
    fake = _Fake()
    assert _run(session_factory, store, fake)["documents_skipped_no_readable_text"] == 1
    monkeypatch.undo()  # the reader is fixed
    summary = _run(session_factory, store, fake)
    assert (summary["documents_read"], summary["claims_stored"]) == (1, 1)
    assert len(fake.calls) == 1


def test_re_checks_never_crowd_out_new_releases(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1", b"<p>no words here</p>")
    _document(store, session_factory, "b", "8-K EX-99.1", b"<p>nor here</p>")
    fake = _Fake()
    _run(session_factory, store, fake)
    _document(store, session_factory, "c", "8-K EX-99.1")
    summary = _run(session_factory, store, fake, limit=1)
    assert (summary["documents_read"], summary["claims_stored"]) == (1, 1)  # the new release, first
    assert summary["documents_rechecked_still_skipped"] == 1  # re-checks have their own limit


def test_fewer_than_fifty_readable_words_is_no_readable_text(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    words = " ".join(["word"] * (MIN_WORDS - 1))
    hidden = "<p style='display:none'>" + "hidden " * 100 + "</p>"  # the model never sees hidden words
    _document(store, session_factory, "a", "8-K EX-99.1", f"<p>{words}</p>{hidden}".encode())
    _document(store, session_factory, "b", "8-K EX-99.1", f"<p>{words} more</p>".encode())
    fake = _Fake()
    summary = _run(session_factory, store, fake)
    assert (summary["documents_read"], summary["documents_skipped_no_readable_text"]) == (1, 1)
    assert len(fake.calls) == 1


def test_a_copy_over_the_input_limit_is_skipped_logged_and_counted(
    session_factory: sessionmaker[Session], tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # 40,000 tokens at four characters each: the largest release that ever gave a catalyst was 19,089.
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1", b"<p>" + b"word " * 32_001 + b"</p>")
    fake = _Fake()
    with caplog.at_level(logging.INFO, logger="catalyst"):
        summary = _run(session_factory, store, fake)
    assert fake.calls == []
    assert (summary["documents_read"], summary["documents_skipped_too_large"]) == (0, 1)
    assert summary["documents_failed"] == 0
    (event,) = [r for r in caplog.records if r.getMessage() == "document_too_large"]
    fields = event.__dict__["catalyst_fields"]
    assert (fields["source_key"], fields["limit"]) == ("sec:a", 40_000)
    assert fields["estimated_tokens"] > 40_000
    assert _readings(session_factory) == [("1.0.0", "too_large", 0)]
    # Checked again each run without a model call, but counted (and so alerted) only once.
    again = _run(session_factory, store, fake)
    assert fake.calls == []
    assert (again["documents_skipped_too_large"], again["documents_rechecked_still_skipped"]) == (0, 1)
    # A higher limit lets it through.
    role = _config().roles["extractor"].model_copy(update={"max_input_tokens": 50_000})
    bigger = _config().model_copy(update={"roles": {"extractor": role}})
    fake.answer = {"catalysts": []}
    assert _run(session_factory, store, fake, config=bigger)["documents_read"] == 1
    assert len(fake.calls) == 1
    # The model's reading replaces the skip, so it is done now and never paid for again.
    assert _readings(session_factory) == [("1.0.0", None, 0)]
    assert _run(session_factory, store, fake, config=bigger)["documents_skipped_already_done"] == 1
    assert len(fake.calls) == 1


def test_a_release_whose_proposals_all_lose_their_quotes_is_not_paid_for_again(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """Optional finding O1 of the Opus review of PR #79: it makes no claim, so it needs a reading."""
    store = LocalSnapshotStore(tmp_path)
    _document(store, session_factory, "a", "8-K EX-99.1")
    lost = ANSWER["catalysts"][0] | {"quote": "Words the release never says."}
    fake = _Fake(answer={"catalysts": [lost]})
    summary = _run(session_factory, store, fake)
    assert (summary["catalysts_proposed"], summary["claims_stored"]) == (1, 0)
    assert _readings(session_factory) == [("1.0.0", None, 1)]
    assert _run(session_factory, store, fake)["documents_skipped_already_done"] == 1
    assert len(fake.calls) == 1
