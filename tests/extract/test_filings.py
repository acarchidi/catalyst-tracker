"""Reading 10-Q, 10-K and other 8-K exhibits part by part (ADR 0066), on a disposable database.

The two filings are built in EDGAR's inline XBRL layout (a hidden `ix:header`, a table of
contents, Part and Item headings); see `filing_pages`.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.extract.filings import (
    USUAL_CAP_STOP,
    SpendGuard,
    filing_view,
    filings_waiting,
    is_filing,
    part_call_most,
    read_filings,
    reader_text,
)
from catalyst.extract.injection import hidden_letters
from catalyst.extract.sections import BUSINESS, MDA, OTHER, WHOLE, document_parts, sections
from catalyst.extract.text import document_text
from catalyst.llm.client import Completion, ModelClient, ModelError
from catalyst.llm.config import ModelConfig, RaisedCap, RoleConfig
from catalyst.llm.ledger import InMemoryLedger
from catalyst.llm.pricing import Price, TokenUsage
from catalyst.llm.prompts import Prompt
from catalyst.schemas import Claim, DocumentPartReading, DocumentReading, SourceDocument, SourceTier
from catalyst.snapshots import LocalSnapshotStore, SnapshotNotFoundError, snapshot_key, verify_snapshots
from catalyst.store_cleanup import clean_store
from catalyst.verify.second_run import run_second_reading
from extract.filing_pages import ANNUAL, QUARTERLY

NOW = datetime(2026, 11, 24, 1, tzinfo=UTC)
TODAY = date(2026, 11, 23)
MDA_SENTENCE = (
    "We completed enrollment in the Phase 3 RAINIER trial of pegozafermin in MASH, and we expect to "
    "report topline data in the first half of 2027."
)
OTHER_SENTENCE = "An end-of-phase 2 meeting for ACM-201 is expected in the first quarter of 2027."
# In the financial statements, which are not read: a reader must never be shown it.
STATEMENTS_SENTENCE = (
    "The topline data from the Phase 3 RAINIER trial are expected in the second half of 2027"
)
RISK_WORDS = "we may decide to stop a program at any time"


def _visible(content: bytes) -> str:
    return filing_view(content, "text/html", "10-Q").visible


def test_a_quarterly_report_is_read_in_its_discussion_and_other_information_only() -> None:
    visible = _visible(QUARTERLY)
    found = sections(visible, "10-Q")
    assert [name for name, _, _ in found] == [MDA, OTHER]
    read = " ".join(visible[start:end] for _, start, end in found)
    assert MDA_SENTENCE in read
    assert OTHER_SENTENCE in read
    assert STATEMENTS_SENTENCE not in read  # financial statements are not read
    assert RISK_WORDS not in read  # nor risk factors
    assert "Unregistered Sales" not in read  # Part II, Item 2 is not Part I, Item 2
    # The table of contents names the discussion too; the section itself is the long one.
    assert visible[found[0][1] :].startswith("Item 2.\nManagement")


def test_an_annual_report_is_read_in_its_business_and_discussion() -> None:
    visible = filing_view(ANNUAL, "text/html", "10-K").visible
    found = sections(visible, "10-K")
    assert [name for name, _, _ in found] == [BUSINESS, MDA]
    read = " ".join(visible[start:end] for _, start, end in found)
    assert "We expect topline data from RAINIER in the first half of 2027." in read
    assert RISK_WORDS not in read


def test_an_exhibit_or_an_unrecognised_filing_is_read_whole() -> None:
    assert sections("Acme Bio presents new data.\nMore words.", "8-K EX-99.2") == [(WHOLE, 0, 39)]
    assert sections("No item headings at all.", "10-Q") == [(WHOLE, 0, 24)]


@pytest.mark.parametrize("limit", [120, 250, 600, 10_000])
def test_parts_are_exact_slices_no_longer_than_the_limit_that_cover_each_section(limit: int) -> None:
    visible = _visible(QUARTERLY)
    parts = document_parts(visible, "10-Q", limit)
    assert all(part.end - part.start <= limit for part in parts)
    assert all(visible[part.start : part.end] == part.text for part in parts)
    assert [part.index for part in parts] == list(range(len(parts)))
    assert {part.count for part in parts} == {len(parts)}
    for name, start, end in sections(visible, "10-Q"):
        covered = "".join(part.text for part in parts if part.section == name)
        assert "".join(visible[start:end].split()) == "".join(covered.split())  # nothing dropped


def test_a_line_longer_than_a_part_is_cut_at_sentence_ends() -> None:
    line = " ".join(f"Sentence number {n} ends here." for n in range(40))
    parts = document_parts(line, "8-K EX-99.2", 200)
    assert len(parts) > 1
    assert all(part.text.rstrip().endswith(".") for part in parts)


def test_which_documents_this_reader_reads() -> None:
    assert is_filing("10-Q")
    assert is_filing("10-K")
    assert is_filing("8-K EX-99.2")
    assert is_filing("8-K EX-99")
    assert not is_filing("8-K EX-99.1")  # press releases are read whole, as before
    assert not is_filing("S-3")
    assert not is_filing("424B5")
    assert not is_filing("Registry record")


def _event(quote: str, date_text: str, drug: str, kind: str = "topline_readout") -> dict[str, Any]:
    return {
        "catalyst_type": kind,
        "title": "An event",
        "drug": drug,
        "indication": None,
        "date_text": date_text,
        "quote": quote,
    }


ANSWERS = {
    MDA_SENTENCE: _event(MDA_SENTENCE, "first half of 2027", "pegozafermin"),
    OTHER_SENTENCE: _event(OTHER_SENTENCE, "first quarter of 2027", "ACM-201", "regulatory_submission"),
}


@dataclass
class _Fake:
    """Answers with the events whose sentence is in the part it is shown."""

    name: str = "fake"
    shown: list[str] = field(default_factory=list[str])
    prompts: list[str] = field(default_factory=list[str])

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.shown.append(document)
        self.prompts.append(prompt.id)
        events = [event for sentence, event in ANSWERS.items() if sentence in " ".join(document.split())]
        return Completion(
            json.dumps({"catalysts": events}), model, "req", "end_turn", TokenUsage(input=500, output=100)
        )


def _config(cap: str = "5", raised: str | None = None) -> ModelConfig:
    price = Price(input=Decimal(2), cache_write=Decimal(3), cache_read=Decimal(0), output=Decimal(10))
    raises = (
        (RaisedCap(cap_usd=Decimal(raised), first_day=TODAY, last_day=TODAY, approved="test"),)
        if raised
        else ()
    )
    role = RoleConfig(
        provider="fake",
        model="claude-sonnet-5-5",
        max_tokens=1000,
        daily_cap_usd=Decimal(cap),
        raised_caps=raises,
    )
    return ModelConfig(
        version="test", pricing={"claude-sonnet-5-5": price}, roles={"extractor": role, "verifier": role}
    )


def _store(
    factory: sessionmaker[Session],
    store: LocalSnapshotStore,
    key: str,
    kind: str,
    content: bytes = QUARTERLY,
    content_type: str = "text/html",
) -> None:
    fetched = FetchedDocument(
        source_key=f"sec:0000000001-26-{key}:{key}.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/1/{key}.htm",
        publisher="SEC EDGAR",
        source_tier=SourceTier.REGULATOR_OR_SEC,
        document_type=kind,
        published_at=datetime(2026, 11, 5, 21, tzinfo=UTC),
        content=content,
        content_type=content_type,
    )
    store_fetched(fetched, store, factory)


def _count(factory: sessionmaker[Session], model: type[Any]) -> int:
    with factory() as session:
        return session.execute(select(func.count()).select_from(TABLES[model])).scalar_one()


def _fully_read(factory: sessionmaker[Session]) -> list[datetime | None]:
    table = TABLES[SourceDocument]
    with factory() as session:
        return list(
            session.execute(select(table.c.fully_read_at).where(table.c.document_type == "10-Q")).scalars()
        )


def test_a_quarterly_report_becomes_checked_claims_part_by_part_and_is_never_read_again(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    _store(session_factory, store, "r", "8-K EX-99.1", content=QUARTERLY.replace(b"Acme", b"Other"))
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=600)
    summary = report.summary()
    assert summary["filings_fully_read"] == 1
    assert summary["filing_parts_read"] == len(fake.shown) == 4  # the press release is not read here
    assert summary["filing_claims_stored"] == 2
    assert summary["filings_waiting"] == 0
    shown = " ".join(fake.shown)
    assert STATEMENTS_SENTENCE not in shown
    assert RISK_WORDS not in shown
    assert all(part.startswith("Publication date: 2026-11-05\n\n") for part in fake.shown)
    # The same claims as a press release's: quote placed in the whole copy's text, problems checked.
    text = document_text(QUARTERLY, "text/html")
    with session_factory() as session:
        claims = session.execute(select(TABLES[Claim])).all()
    for claim in claims:
        assert text[claim.quote_start : claim.quote_end] == claim.quote
        assert claim.value["problems"] == []  # the hidden XBRL header does not hold every claim
        assert claim.agent == "extractor"
        assert claim.prompt_version == "1.0.0"
    assert _fully_read(session_factory) == [NOW]
    with session_factory() as session:
        reading = session.execute(select(TABLES[DocumentReading])).one()
    assert (reading.agent, reading.catalysts, reading.skipped) == ("extractor", 2, None)
    again = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=600)
    assert len(fake.shown) == 4  # never read twice
    assert again.summary()["filings_started"] == 0
    assert _count(session_factory, Claim) == 2


def test_a_run_that_stops_part_way_goes_on_with_the_next_part(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    fake = _Fake()
    clock = [NOW]

    def now() -> datetime:
        clock[0] += timedelta(minutes=1)
        return clock[0]

    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    first = read_filings(
        session_factory, store, client, limit=10, now=now, deadline=NOW + timedelta(minutes=4), part_chars=600
    )
    assert first.summary()["stopped_for_time"] is True
    read_first = first.summary()["filing_parts_read"]
    assert 0 < read_first < 4
    assert _fully_read(session_factory) == [None]
    assert _count(session_factory, DocumentPartReading) == read_first
    second = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=600)
    assert second.summary()["filing_parts_read"] == 4 - read_first
    assert second.summary()["filing_parts_already_read"] == read_first
    assert len(fake.shown) == 4  # every part paid for once
    assert _fully_read(session_factory) == [NOW]


def test_a_page_that_hides_other_words_still_holds_every_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    hidden = QUARTERLY.replace(
        b"<p>Recent Developments.",
        b'<p style="display:none">Hidden words for a reader.</p><p>Recent Developments.',
    )
    _store(session_factory, store, "q", "10-Q", content=hidden)
    client = ModelClient(_config(), {"fake": _Fake()}, InMemoryLedger(), now=lambda: NOW)
    read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=600)
    with session_factory() as session:
        problems = [row.value["problems"] for row in session.execute(select(TABLES[Claim])).all()]
    assert problems
    assert all("source_hides_text" in found for found in problems)


def test_a_pdf_exhibit_is_kept_as_a_reading_that_no_model_read_and_is_never_fully_read(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(
        session_factory, store, "d", "8-K EX-99.2", content=b"%PDF-1.7 slides", content_type="application/pdf"
    )
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = read_filings(session_factory, store, client, limit=10, now=lambda: NOW)
    assert report.summary()["filings_skipped_not_text"] == 1
    assert fake.shown == []
    table = TABLES[SourceDocument]
    with session_factory() as session:
        assert session.execute(select(table.c.fully_read_at)).scalar_one() is None
        assert filings_waiting(session) == 0
    read_filings(session_factory, store, client, limit=10, now=lambda: NOW)
    assert fake.shown == []


def test_filings_spend_only_the_usual_cap_even_on_a_raised_cap_day(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    """The raised $8 and $4 caps are the press-release backfill's alone (owner, 2026-11-23)."""
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    config = _config(cap="2", raised="8")  # TODAY is a raised-cap day
    ledger = InMemoryLedger()
    ledger._extra[("extractor", TODAY)] = Decimal("1.95")  # pyright: ignore[reportPrivateUsage]
    fake = _Fake()
    client = ModelClient(config, {"fake": fake}, ledger, now=lambda: NOW)
    guard = SpendGuard(config, ledger, TODAY)
    report = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, guard=guard)
    assert report.summary()["stopped_early"] == USUAL_CAP_STOP
    assert fake.shown == []  # $1.95 + one part's worst case passes the usual $2, though $8 is open
    assert client_would_allow(config, ledger)  # the client alone would have let it spend


def client_would_allow(config: ModelConfig, ledger: InMemoryLedger) -> bool:
    """Say whether the day's (raised) cap has room left, as the model client sees it."""
    return ledger.spent("extractor", TODAY) < config.roles["extractor"].cap_on(TODAY)


def test_one_part_s_worst_case_comes_from_the_role_s_prices() -> None:
    config = _config(cap="2")
    worst = part_call_most(config, "extractor")
    # (80,000 + 8,000 characters) / 3 tokens at $2 a million, plus 1,000 output tokens at $10 a million.
    assert worst == Decimal(29334) * 2 / 1_000_000 + Decimal(1000) * 10 / 1_000_000


def test_the_second_reading_reads_only_the_parts_that_hold_a_claim(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    fake = _Fake()
    config = _config()
    client = ModelClient(config, {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    report = run_second_reading(
        session_factory, store, client, config, limit=10, now=lambda: NOW, gate=lambda _c: []
    )
    assert fake.prompts == []  # not before the filing is fully read
    assert report.summary()["documents_read"] == 0
    read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=80_000)
    extracted = len(fake.shown)
    report = run_second_reading(
        session_factory, store, client, config, limit=10, now=lambda: NOW, gate=lambda _c: []
    )
    verifier_parts = fake.prompts[extracted:]
    assert verifier_parts == ["verify_catalysts", "verify_catalysts"]  # MD&A and Other information
    assert report.summary()["documents_read"] == 1
    assert report.summary()["claims_by_outcome"]["agrees"] == 2
    again = run_second_reading(
        session_factory, store, client, config, limit=10, now=lambda: NOW, gate=lambda _c: []
    )
    assert again.summary()["documents_read"] == 0
    assert len(fake.prompts) == extracted + 2


@settings(max_examples=200, deadline=None)
@given(
    lines=st.lists(
        st.text(alphabet=st.characters(codec="ascii", exclude_characters="\n"), max_size=300), max_size=40
    ),
    limit=st.integers(min_value=20, max_value=2_000),
)
def test_any_text_is_cut_into_exact_slices_under_the_limit_that_drop_no_words(
    lines: list[str], limit: int
) -> None:
    text = "\n".join(lines)
    parts = document_parts(text, "8-K EX-99.2", limit)
    assert all(0 < part.end - part.start <= limit for part in parts)
    assert all(text[part.start : part.end] == part.text for part in parts)
    assert all(a.end <= b.start for a, b in pairwise(parts))  # in order, never overlapping
    assert "".join(text.split()) == "".join("".join(part.text for part in parts).split())


def test_the_weekly_sample_is_shown_the_part_that_holds_the_quote() -> None:
    """ADR 0041's checker reads at most 30,000 tokens: a filing's claim is checked in its own part."""
    part = reader_text(QUARTERLY, "text/html", "10-Q", OTHER_SENTENCE)
    assert OTHER_SENTENCE in " ".join(part.split())
    assert MDA_SENTENCE not in " ".join(part.split())  # the parts are small enough here to be apart
    release = reader_text(QUARTERLY, "text/html", "8-K EX-99.1", OTHER_SENTENCE)
    assert STATEMENTS_SENTENCE in " ".join(release.split())  # a press release is shown whole, as before


def test_a_fully_read_exhibit_with_no_catalyst_loses_its_copy_and_a_filing_with_claims_keeps_it(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    nothing = b"<p>" + b"Acme Bio will hold its annual meeting of stockholders as planned. " * 10 + b"</p>"
    _store(session_factory, store, "x", "8-K EX-99.2", content=nothing)
    client = ModelClient(_config(), {"fake": _Fake()}, InMemoryLedger(), now=lambda: NOW)
    read_filings(session_factory, store, client, limit=10, now=lambda: NOW)
    report = clean_store(session_factory, store, delete=True, now=lambda: NOW)
    assert report.summary()["by_document_type"] == {
        "10-Q": {"kept_backs_a_claim": 1},
        "8-K EX-99.2": {"deleted": 1},
    }
    with pytest.raises(SnapshotNotFoundError):
        store.get(snapshot_key(hashlib.sha256(nothing).hexdigest()))
    store.get(snapshot_key(hashlib.sha256(QUARTERLY).hexdigest()))  # the evidence stays
    verified = verify_snapshots(session_factory, store)
    assert verified.ok  # a copy deleted on purpose is not missing
    assert (verified.checked, verified.deleted) == (1, 1)


@dataclass
class _Failing(_Fake):
    """Fails the call numbered `fail_at` (from 1), then answers as `_Fake`."""

    fail_at: int = 2
    calls: int = 0

    def complete(
        self, *, model: str, prompt: Prompt, document: str, max_tokens: int, effort: str | None
    ) -> Completion:
        self.calls += 1
        if self.calls == self.fail_at:
            raise ModelError("the provider is down")
        return super().complete(
            model=model, prompt=prompt, document=document, max_tokens=max_tokens, effort=effort
        )


def test_a_failed_call_fails_that_filing_and_the_next_run_reads_only_what_is_left(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    fake = _Failing()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    first = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=600)
    assert (first.summary()["filings_failed"], first.summary()["filing_parts_read"]) == (1, 1)
    assert _fully_read(session_factory) == [None]
    second = read_filings(session_factory, store, client, limit=10, now=lambda: NOW, part_chars=600)
    assert second.summary()["filing_parts_read"] == 3
    assert _fully_read(session_factory) == [NOW]


def test_a_missing_copy_fails_and_a_copy_with_almost_no_words_is_skipped_for_free(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "s", "8-K EX-99.2", content=b"<p>Slides follow.</p>")
    _store(session_factory, store, "q", "10-Q")
    (tmp_path / snapshot_key(hashlib.sha256(QUARTERLY).hexdigest())).unlink()
    fake = _Fake()
    client = ModelClient(_config(), {"fake": fake}, InMemoryLedger(), now=lambda: NOW)
    summary = read_filings(session_factory, store, client, limit=10, now=lambda: NOW).summary()
    assert (summary["filings_failed"], summary["filings_skipped_no_readable_text"]) == (1, 1)
    assert fake.shown == []
    again = read_filings(session_factory, store, client, limit=10, now=lambda: NOW).summary()
    assert again["filings_rechecked_still_skipped"] == 1  # re-checked for free, counted once


def test_the_second_reading_of_filings_pauses_at_the_usual_cap_without_failing(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, "q", "10-Q")
    config = _config(cap="2")
    ledger = InMemoryLedger()
    fake = _Fake()
    client = ModelClient(config, {"fake": fake}, ledger, now=lambda: NOW)
    read_filings(session_factory, store, client, limit=10, now=lambda: NOW)
    ledger._extra[("verifier", TODAY)] = Decimal("1.95")  # pyright: ignore[reportPrivateUsage]
    guard = SpendGuard(config, ledger, TODAY)
    report = run_second_reading(
        session_factory, store, client, config, limit=10, now=lambda: NOW, gate=lambda _c: [], guard=guard
    )
    summary = report.summary()
    assert summary["filings_paused"] == USUAL_CAP_STOP
    assert summary["stopped_early"] is None  # a pause is not a stop at a cap
    assert "verify_catalysts" not in fake.prompts


REAL_TEXT = "<p>" + "Acme Bio develops medicines for serious liver diseases in clinical trials. " * 8 + "</p>"


def _hides(page: str) -> bool:
    content = f"<html><body>{page}{REAL_TEXT}</body></html>".encode()
    view = filing_view(content, "text/html", "10-Q")
    return hidden_letters(view.hide_text, view.visible)


def test_a_real_inline_xbrl_header_is_not_hidden_text() -> None:
    header = (
        '<div style="display:none"><ix:header><ix:hidden><ix:nonNumeric name="dei:AmendmentFlag">false'
        '</ix:nonNumeric></ix:hidden><ix:resources><xbrli:context id="c"><xbrli:entity>Q3</xbrli:entity>'
        "</xbrli:context></ix:resources></ix:header></div>"
    )
    assert not _hides(header)
    assert not hidden_letters(filing_view(QUARTERLY, "text/html", "10-Q").hide_text, _visible(QUARTERLY))


@pytest.mark.parametrize(
    "page",
    [
        # Header words inside comments are no element: what lies between them stays hidden text.
        '<!-- <ix:header> --><div style="display:none">Secret words for a model</div><!-- </ix:header> -->',
        # In a script, likewise.
        '<script>"<ix:header>"</script><div style="display:none">Secret words</div>'
        '<script>"</ix:header>"</script>',
        # A bare header with no hidden ancestor, wrapping a hidden block.
        '<ix:header><div style="display:none">Secret words for a model</div></ix:header>',
        # A hidden header that holds page markup, not only data tags.
        '<div style="display:none"><ix:header><div>Secret words for a model</div></ix:header></div>',
        # A second header: a filing has one.
        '<div style="display:none"><ix:header><ix:hidden>a</ix:hidden></ix:header></div>'
        '<div style="display:none"><ix:header><ix:hidden>Secret words</ix:hidden></ix:header></div>',
        # A hidden block beside a real header is still hidden text.
        '<div style="display:none"><ix:header><ix:hidden>false</ix:hidden></ix:header></div>'
        '<div style="display:none">Secret words for a model</div>',
    ],
)
def test_hidden_text_dressed_as_an_xbrl_header_still_holds_every_claim(page: str) -> None:
    """Opus review of PR #150, B1: only a real, hidden, data-only header is left out of the check."""
    assert _hides(page)
