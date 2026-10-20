"""The prototype export (ADR 0034): published catalysts as the read API shows them; held claims apart."""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import insert, select
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import BACKED, FIRST, UNPUBLISHED_QUOTE, Seed
from catalyst import cli
from catalyst.api.access import NOTICE, Access
from catalyst.api.read import catalyst, read_session
from catalyst.db.tables import TABLES
from catalyst.export import HELD_STATUS, PARAGRAPH_CONTEXT, build_export, paragraph
from catalyst.extract.text import document_text
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystStatus,
    CatalystType,
    Claim,
    ClaimDecision,
    ClaimSubject,
    DateConfidence,
    DatePrecision,
    Entity,
    EntityMatch,
    GateDecision,
    MatchedEntity,
    MatchStatus,
    SourceDocument,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore

NOW = datetime(2026, 10, 18, 14, 30, tzinfo=UTC)
HELD_REASONS = ["independent_source: no second source agrees", "check type_named_in_quote has not run"]


def _export(factory: sessionmaker[Session], *, include_held: bool = False) -> dict[str, Any]:
    with read_session(factory) as session:
        return build_export(session, NOW, include_held=include_held)


def _ids(export: dict[str, Any]) -> list[UUID]:
    return [UUID(item["id"]) for item in export["catalysts"]]


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def _claim_of(session: Session, catalyst_id: UUID) -> UUID:
    change = TABLES[CatalystChange]
    query = select(change.c.claim_id).where(change.c.catalyst_id == catalyst_id).limit(1)
    return session.execute(query).scalar_one()


def _hold(factory: sessionmaker[Session], seed: Seed) -> None:
    """Store a review decision on the unpublished claim, matched to Other Therapeutics."""
    with factory.begin() as session:
        _add(
            session,
            ClaimDecision(
                claim_id=seed.unpublished_claim,
                decision=GateDecision.REVIEW,
                reasons=HELD_REASONS,
                decided_at=FIRST,
            ),
        )
        _add(
            session,
            EntityMatch(
                claim_id=seed.unpublished_claim,
                entity_type=MatchedEntity.COMPANY,
                name="https://www.sec.gov/Archives/edgar/data/1111/000001/ex99-1.htm",
                status=MatchStatus.RESOLVED,
                matched_id=seed.other,
                candidates=[str(seed.other)],
                resolved_at=FIRST,
            ),
        )
        # A claim the gate let through is never listed as held.
        published_claim = _claim_of(session, seed.pdufa)
        _add(
            session,
            ClaimDecision(
                claim_id=published_claim, decision=GateDecision.PUBLISH, reasons=[], decided_at=FIRST
            ),
        )


def test_published_catalysts_are_exactly_what_the_read_api_returns(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    export = _export(session_factory)
    # Every published one, resolved too, in window order; never the one missing a source.
    assert _ids(export) == [seed.slipped, seed.resolved, seed.adcom, seed.pdufa]
    with read_session(session_factory) as session:
        for item in export["catalysts"]:
            shown = catalyst(session, UUID(item["id"]), Access.PAID)
            assert shown is not None
            assert item == shown.catalyst.model_dump(mode="json")


def test_a_record_with_a_value_that_has_no_source_is_left_out(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    export = _export(session_factory)
    assert seed.unsourced not in _ids(export)
    assert UNPUBLISHED_QUOTE not in json.dumps(export)


def test_every_source_names_its_claim_document_date_and_quote(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    export = _export(session_factory)
    claim, document = TABLES[Claim], TABLES[SourceDocument]
    with session_factory() as session:
        stored = {
            row.id: (row.quote, row.url)
            for row in session.execute(
                select(claim.c.id, claim.c.quote, document.c.url).join(
                    document, document.c.id == claim.c.source_document_id
                )
            )
        }
    slipped = export["catalysts"][0]
    assert slipped["company"]["name"] == "Example Bio, Inc."
    assert slipped["company"]["ticker"] == "EXB"
    assert [d["name"] for d in slipped["drugs"]] == ["exampumab"]
    assert slipped["window"]["raw_text"] == "4Q26"
    for item in export["catalysts"]:
        for source in item["sources"]:
            assert stored[UUID(source["claim_id"])] == (source["quote"], source["url"])
            assert source["published_at"]
        shown = {name for source in item["sources"] for name in source["fields"]}
        assert {"catalyst_type", "title", "window_start", "window_end", "status"} <= shown


def test_the_envelope_has_the_notice_counts_and_prompt_versions(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    export = _export(session_factory)
    assert list(export) == ["notice", "generated_at", "counts", "prompt_versions", "catalysts"]
    assert export["notice"] == NOTICE
    assert export["generated_at"] == "2026-10-18T14:30:00+00:00"
    assert export["counts"] == {"published": 4, "held": None, "companies": 2}
    assert export["prompt_versions"] == {
        "extractor": "extract_catalysts/1.0.0",
        "verifier": "verify_catalysts/1.0.0",
    }
    assert "held" not in export


def test_held_claims_are_listed_apart_with_their_reasons(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    _hold(session_factory, seed)
    plain = _export(session_factory)
    export = _export(session_factory, include_held=True)
    assert export["catalysts"] == plain["catalysts"]
    assert export["counts"] == {"published": 4, "held": 1, "companies": 2}
    [held] = export["held"]
    assert held["claim_id"] == str(seed.unpublished_claim)
    assert held["status"] == HELD_STATUS == "held for review"
    assert held["reasons"] == HELD_REASONS
    assert held["quote"] == UNPUBLISHED_QUOTE
    assert held["values"] == {"quote": UNPUBLISHED_QUOTE}
    assert held["company"] == {
        "id": str(seed.other),
        "name": "Other Therapeutics",
        "cik": 2222,
        "ticker": "OTH",
    }
    assert held["source"]["url"] == "https://www.sec.gov/Archives/edgar/data/1111/000001/ex99-1.htm"
    assert held["prompt"] == "extract_catalysts/1.0.0"
    # Nothing held reaches the published list.
    assert UNPUBLISHED_QUOTE not in json.dumps(export["catalysts"])


def test_a_held_claim_with_no_single_company_says_so(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with session_factory.begin() as session:
        _add(
            session,
            ClaimDecision(
                claim_id=seed.unpublished_claim, decision=GateDecision.REVIEW, reasons=["x"], decided_at=FIRST
            ),
        )
    [held] = _export(session_factory, include_held=True)["held"]
    assert held["company"] is None


def _publish(session: Session, company: UUID, start: date) -> UUID:
    """Publish a one-day advisory committee, every value backed by one new claim."""
    document = session.execute(select(TABLES[SourceDocument].c.id).limit(1)).scalar_one()
    quote = f"an advisory committee on {start.isoformat()}"
    claim = Claim(
        source_document_id=document,
        subject_type=ClaimSubject.CATALYST,
        field=f"catalyst:{uuid4()}",
        value={"quote": quote},
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=FIRST,
    )
    _add(session, claim)
    record = Catalyst(
        catalyst_type=CatalystType.ADVISORY_COMMITTEE,
        company_id=company,
        title=f"Advisory committee on {start.isoformat()}",
        window_start=start,
        window_end=start,
        window_precision=DatePrecision.DAY,
        window_raw_text=start.isoformat(),
        date_confidence=DateConfidence.REGULATOR_SCHEDULED,
        status=CatalystStatus.UPCOMING,
    )
    _add(session, record)
    values = record.model_dump(mode="json")
    for field in BACKED:
        if values[field.value] is not None:
            _add(
                session,
                CatalystChange(
                    catalyst_id=record.id,
                    field=field,
                    new_value=values[field.value],
                    claim_id=claim.id,
                    learned_at=FIRST,
                ),
            )
    return record.id


def test_order_is_window_start_then_company_then_id(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    day = date(2026, 11, 5)  # the same day as Other Therapeutics' advisory committee
    with session_factory.begin() as session:
        added = [_publish(session, seed.example, day) for _ in range(3)]
    ids = _ids(_export(session_factory))
    # Example Bio sorts before Other Therapeutics on the same day; ties go by id.
    assert ids == [seed.slipped, seed.resolved, *sorted(added, key=str), seed.adcom, seed.pdufa]
    first = _export(session_factory, include_held=True)
    assert json.dumps(first) == json.dumps(_export(session_factory, include_held=True))


def test_the_command_writes_the_file_and_prints_the_counts(
    session_factory: sessionmaker[Session],
    seed: Seed,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _hold(session_factory, seed)
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    out = tmp_path / "export.json"
    assert cli.main(["export-catalysts", "--out", str(out), "--include-held"]) == 0
    printed = capsys.readouterr().out
    assert printed.count("\n") == 1
    assert json.loads(printed) == {"published": 4, "held": 1, "companies": 2}
    text = out.read_text(encoding="utf-8")
    # The job shows standard output in its summary, so it carries counts only: no claim or quote.
    with session_factory() as session:
        claims = session.execute(select(TABLES[Claim].c.id, TABLES[Claim].c.quote)).all()
    assert claims
    for claim_id, quote in claims:
        assert str(claim_id) not in printed
        assert quote not in printed
    assert UNPUBLISHED_QUOTE in text  # the held claim is in the file, only there
    assert text.startswith('{\n "notice": ')  # indent 1
    written = json.loads(text)
    assert _ids(written) == [seed.slipped, seed.resolved, seed.adcom, seed.pdufa]
    assert [h["claim_id"] for h in written["held"]] == [str(seed.unpublished_claim)]
    assert list(tmp_path.iterdir()) == [out]  # no temporary file left behind


def test_the_command_without_the_flag_writes_no_held_list(
    session_factory: sessionmaker[Session],
    seed: Seed,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _hold(session_factory, seed)
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    out = tmp_path / "export.json"
    assert cli.main(["export-catalysts", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == {"published": 4, "held": None, "companies": 2}
    assert "held" not in json.loads(out.read_text(encoding="utf-8"))


def test_an_unreachable_database_is_one_plain_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:1/nothing_test")
    assert cli.main(["export-catalysts", "--out", str(tmp_path / "x.json")]) == 1
    captured = capsys.readouterr()
    assert "cannot reach the database" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not (tmp_path / "x.json").exists()


def test_the_command_without_a_database_setting_names_the_setup_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv("CATALYST_DATABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres@db.example.supabase.co:5432/postgres")
    assert cli.main(["export-catalysts", "--out", str(tmp_path / "x.json")]) == 2
    assert "docs/setup.md" in capsys.readouterr().err
    assert not (tmp_path / "x.json").exists()


# --- Paragraphs and the review-decision slot for held claims (ADR 0034, amended 2026-10-20) ---

RELEASE = (
    b"<html><body><p>Acme Bio today reported results.</p>"
    b"<p>The company expects to submit the NDA in 2H26. It also plans a meeting with the FDA.</p>"
    b"<p>About Acme Bio</p></body></html>"
)
HELD_QUOTE = "expects to submit the NDA in 2H26"


def _held_on_a_real_copy(factory: sessionmaker[Session], store: LocalSnapshotStore, quote: str) -> UUID:
    """Store RELEASE, a document for it, and a held claim quoting it at its place in the text."""
    snapshot = store.put(RELEASE)
    text = document_text(RELEASE, "text/html")
    start = max(text.find(quote), 0)
    doc = SourceDocument(
        source_key="wire:acme:1",
        url="https://example.com/acme-1",
        publisher="PR Newswire",
        source_tier=SourceTier.WIRE,
        document_type="press release",
        published_at=FIRST,
        retrieved_at=FIRST,
        sha256=snapshot.sha256,
        snapshot_key=snapshot.key,
        content_type="text/html",
    )
    claim = Claim(
        source_document_id=doc.id,
        subject_type=ClaimSubject.CATALYST,
        field="window",
        value={"quote": quote},
        quote=quote,
        quote_start=start,
        quote_end=start + len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=FIRST,
    )
    decision = ClaimDecision(claim_id=claim.id, decision=GateDecision.REVIEW, reasons=["x"], decided_at=FIRST)
    with factory.begin() as session:
        _add(session, doc)
        _add(session, claim)
        _add(session, decision)
    return claim.id


def _held_item(export: dict[str, Any], claim_id: UUID) -> dict[str, Any]:
    [item] = [h for h in export["held"] if h["claim_id"] == str(claim_id)]
    return item


def test_a_held_claim_carries_the_paragraph_its_quote_sits_in(
    session_factory: sessionmaker[Session], seed: Seed, tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    claim_id = _held_on_a_real_copy(session_factory, store, HELD_QUOTE)
    with read_session(session_factory) as session:
        export = build_export(session, NOW, include_held=True, store=store)
    item = _held_item(export, claim_id)
    assert item["paragraph"] == (
        "The company expects to submit the NDA in 2H26. It also plans a meeting with the FDA."
    )
    assert item["paragraph_missing"] is None
    assert item["review_decision"] is None  # filled by the review queue (PR #84)


def test_without_a_store_the_paragraph_is_null_with_the_reason(
    session_factory: sessionmaker[Session], seed: Seed, tmp_path: Path
) -> None:
    claim_id = _held_on_a_real_copy(session_factory, LocalSnapshotStore(tmp_path), HELD_QUOTE)
    item = _held_item(_export(session_factory, include_held=True), claim_id)
    assert item["paragraph"] is None
    assert item["paragraph_missing"] == "no snapshot store was given"


def test_a_missing_copy_gives_a_null_paragraph_with_the_reason(
    session_factory: sessionmaker[Session], seed: Seed, tmp_path: Path
) -> None:
    _hold(session_factory, seed)  # the seed's documents have no stored copy
    with read_session(session_factory) as session:
        export = build_export(session, NOW, include_held=True, store=LocalSnapshotStore(tmp_path))
    item = _held_item(export, seed.unpublished_claim)
    assert item["paragraph"] is None
    assert item["paragraph_missing"] == "the stored copy is missing"


def test_a_quote_not_at_its_place_gives_no_paragraph(
    session_factory: sessionmaker[Session], seed: Seed, tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    claim_id = _held_on_a_real_copy(session_factory, store, "words that are not in the release")
    with read_session(session_factory) as session:
        item = _held_item(build_export(session, NOW, include_held=True, store=store), claim_id)
    assert item["paragraph"] is None
    assert item["paragraph_missing"] == "the quote is not at its stored place in the copy"


def test_a_long_paragraph_is_cut_around_the_quote() -> None:
    before, after = "a" * (PARAGRAPH_CONTEXT + 50), "b" * (PARAGRAPH_CONTEXT + 50)
    text = f"first line\n{before}QUOTE{after}\nlast line"
    start = text.index("QUOTE")
    found = paragraph(text, "QUOTE", start, start + 5)
    assert found == "…" + "a" * PARAGRAPH_CONTEXT + "QUOTE" + "b" * PARAGRAPH_CONTEXT + "…"


def test_a_short_paragraph_is_whole_and_the_quote_may_span_lines() -> None:
    text = "one\ntwo QUOTE\nGOES ON three\nfour"
    start = text.index("QUOTE")
    quote = "QUOTE\nGOES ON"
    assert paragraph(text, quote, start, start + len(quote)) == "two QUOTE\nGOES ON three"
    assert paragraph(text, "QUOTE", 0, 5) is None
    assert paragraph("QUOTE", "QUOTE", 0, 5) == "QUOTE"


def test_the_command_reads_paragraphs_from_the_chosen_store(
    session_factory: sessionmaker[Session],
    seed: Seed,
    database_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    copies = tmp_path / "copies"
    claim_id = _held_on_a_real_copy(session_factory, LocalSnapshotStore(copies), HELD_QUOTE)
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    out = tmp_path / "export.json"
    args = ["export-catalysts", "--out", str(out), "--include-held", "--snapshot-dir", str(copies)]
    assert cli.main(args) == 0
    assert "2H26" not in capsys.readouterr().out  # counts only
    item = _held_item(json.loads(out.read_text(encoding="utf-8")), claim_id)
    assert item["paragraph"].startswith("The company expects")


def test_the_command_without_supabase_settings_names_them(
    database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    out = tmp_path / "export.json"
    args = ["export-catalysts", "--out", str(out), "--include-held", "--snapshot-store", "supabase"]
    assert cli.main(args) == 2
    assert "SUPABASE" in capsys.readouterr().err
    assert not out.exists()
