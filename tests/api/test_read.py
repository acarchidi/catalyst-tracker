"""The read layer shows only published values, each with its source and quote (spec: Provenance)."""

import logging
from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, event, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import FIRST, MOVED, MOVED_AGAIN, TODAY, UNPUBLISHED_QUOTE, Seed, drugs_value
from catalyst.api.access import Access
from catalyst.api.read import Query, catalyst, company_catalysts, read_session, upcoming
from catalyst.api.views import CatalystDetail
from catalyst.db.tables import TABLES
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystDrug,
    CatalystField,
    CatalystType,
    Claim,
    ClaimSubject,
    Company,
    Drug,
    DrugRole,
    Entity,
)


def _ids(page: Any) -> list[Any]:
    return [c.id for c in page.catalysts]


def test_free_calendar_shows_the_next_30_days_of_sourced_upcoming_catalysts(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        page = upcoming(session, Query(), Access.FREE, TODAY)
    # Not the March 2027 date (beyond 30 days), the unsourced one, or the resolved one.
    assert _ids(page) == [seed.slipped, seed.adcom]
    assert page.next_cursor is None


def test_paid_calendar_shows_the_full_horizon(session_factory: sessionmaker[Session], seed: Seed) -> None:
    with read_session(session_factory) as session:
        page = upcoming(session, Query(), Access.PAID, TODAY)
    assert _ids(page) == [seed.slipped, seed.adcom, seed.pdufa]


def test_every_value_carries_the_source_of_its_latest_change(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        result = catalyst(session, seed.slipped, Access.FREE)
    assert result is not None
    found = result.catalyst
    assert found.window.raw_text == "4Q26"
    by_quote = {s.quote: set(s.fields) for s in found.sources}
    assert by_quote["topline data now expected in the fourth quarter of 2026"] == {
        "window_start",
        "window_end",
        "window_raw_text",
        "status",
    }
    first = by_quote["topline data in the third quarter of 2026"]
    assert {"catalyst_type", "company_id", "title", "window_precision", "date_confidence", "drugs"} == first
    assert all(s.url.startswith("https://www.sec.gov/Archives/") for s in found.sources)
    assert [d.name for d in found.drugs] == ["exampumab"]


def test_free_sees_only_the_latest_date_change_and_paid_sees_all(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        free = catalyst(session, seed.slipped, Access.FREE)
        paid = catalyst(session, seed.slipped, Access.PAID)
    assert free is not None
    assert paid is not None
    assert {c.field for c in free.catalyst.latest_date_change} == {
        "window_start",
        "window_end",
        "window_raw_text",
    }
    assert all(c.learned_at == MOVED for c in free.catalyst.latest_date_change)
    assert "history" not in free.model_dump(mode="json")["catalyst"]
    assert isinstance(paid.catalyst, CatalystDetail)
    assert len(paid.catalyst.history) == 10 + 4  # nine first values and the drug link, four moved


def test_a_catalyst_missing_a_source_is_never_shown(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        assert catalyst(session, seed.unsourced, Access.PAID) is None
        assert seed.unsourced not in _ids(upcoming(session, Query(), Access.PAID, TODAY))


def test_a_value_its_latest_source_does_not_state_is_never_shown(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with session_factory.begin() as session:
        table = TABLES[Catalyst]
        session.execute(update(table).where(table.c.id == seed.adcom).values(title="Edited by hand"))
    with read_session(session_factory) as session:
        assert catalyst(session, seed.adcom, Access.PAID) is None


def test_unpublished_claims_never_appear(session_factory: sessionmaker[Session], seed: Seed) -> None:
    with read_session(session_factory) as session:
        pages = [
            upcoming(session, Query(), Access.PAID, TODAY),
            company_catalysts(session, "EXB", Query(), Access.PAID, TODAY),
            catalyst(session, seed.slipped, Access.PAID),
        ]
    for page in pages:
        assert page is not None
        assert UNPUBLISHED_QUOTE not in page.model_dump_json()


@pytest.fixture
def statements(engine: Engine) -> Iterator[list[str]]:
    """Every SQL statement sent while the test runs."""
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine, "before_cursor_execute", record)


def test_never_reads_review_or_check_tables(
    session_factory: sessionmaker[Session], seed: Seed, statements: list[str]
) -> None:
    with read_session(session_factory) as session:
        upcoming(session, Query(drug="exampumab"), Access.PAID, TODAY)
        company_catalysts(session, "1111", Query(), Access.PAID, TODAY)
        catalyst(session, seed.slipped, Access.PAID)
    assert statements
    for statement in statements:
        for table in ("claim_check", "claim_decision", "entity_match", "document_reading", "model_call"):
            assert table not in statement


def test_read_session_refuses_writes(session_factory: sessionmaker[Session]) -> None:
    company = Company(cik=3333, name="Should Not Exist")
    with pytest.raises(DBAPIError, match="read-only"), read_session(session_factory) as session:
        session.execute(insert(TABLES[Company]).values(**company.model_dump()))


@pytest.mark.parametrize("key", ["EXB", "exb", "1111"])
def test_company_found_by_ticker_or_sec_number(
    session_factory: sessionmaker[Session], seed: Seed, key: str
) -> None:
    with read_session(session_factory) as session:
        page = company_catalysts(session, key, Query(), Access.PAID, TODAY)
    assert page is not None
    assert page.company.name == "Example Bio, Inc."
    assert page.company.ticker == "EXB"
    assert _ids(page) == [seed.slipped, seed.pdufa]


def test_company_found_by_id_and_unknown_company_is_none(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        page = company_catalysts(session, str(seed.other), Query(), Access.FREE, TODAY)
        assert page is not None
        assert _ids(page) == [seed.adcom]
        assert company_catalysts(session, "NOPE", Query(), Access.FREE, TODAY) is None
        assert company_catalysts(session, str(uuid4()), Query(), Access.FREE, TODAY) is None
        assert catalyst(session, uuid4(), Access.FREE) is None


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        (Query(drug="exampumab"), ["slipped"]),
        (Query(drug="exb-101"), ["slipped"]),
        (Query(drug="no such drug"), []),
        (Query(types=(CatalystType.ADVISORY_COMMITTEE,)), ["adcom"]),
        (Query(types=(CatalystType.PDUFA_DATE, CatalystType.TOPLINE_READOUT)), ["slipped", "pdufa"]),
        (Query(company="OTH"), ["adcom"]),
        (Query(company="NOPE"), []),
        (Query(start=date(2026, 11, 1), end=date(2026, 11, 30)), ["slipped", "adcom"]),
        (Query(start=date(2026, 11, 6), end=date(2026, 11, 30)), ["slipped"]),
        (Query(start=date(2027, 1, 1)), ["pdufa"]),
    ],
)
def test_filters(
    session_factory: sessionmaker[Session], seed: Seed, query: Query, expected: list[str]
) -> None:
    with read_session(session_factory) as session:
        page = upcoming(session, query, Access.PAID, TODAY)
    assert _ids(page) == [getattr(seed, name) for name in expected]


def test_drug_filter_by_id(session_factory: sessionmaker[Session], seed: Seed) -> None:
    with read_session(session_factory) as session:
        page = upcoming(session, Query(drug=str(seed.drug)), Access.PAID, TODAY)
    assert _ids(page) == [seed.slipped]


def test_cursor_pages_through_in_date_order(session_factory: sessionmaker[Session], seed: Seed) -> None:
    seen: list[Any] = []
    cursor: str | None = None
    with read_session(session_factory) as session:
        for _ in range(4):
            page = upcoming(session, Query(limit=1, cursor=cursor), Access.PAID, TODAY)
            seen += _ids(page)
            cursor = page.next_cursor
            if cursor is None:
                break
    assert seen == [seed.slipped, seed.adcom, seed.pdufa]


@pytest.mark.parametrize("cursor", ["not a cursor", "MjAyNi0xMC0wMQ", ""])
def test_bad_cursor_is_refused(session_factory: sessionmaker[Session], cursor: str) -> None:
    with read_session(session_factory) as session, pytest.raises(ValueError, match="cursor"):
        upcoming(session, Query(cursor=cursor), Access.PAID, TODAY)


@pytest.mark.parametrize("limit", [0, 201])
def test_limit_is_bounded(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        Query(limit=limit)


def test_free_and_paid_page_say_which_window_they_show(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with read_session(session_factory) as session:
        free = upcoming(session, Query(), Access.FREE, TODAY)
        paid = upcoming(session, Query(), Access.PAID, TODAY)
    assert (free.window_from, free.window_to) == (TODAY, date(2026, 11, 16))
    assert (paid.window_from, paid.window_to) == (TODAY, None)
    assert free.access == Access.FREE


def _add(session: Session, record: Entity) -> None:
    session.execute(insert(TABLES[type(record)]).values(**record.model_dump()))


def _second_drug(session: Session, seed: Seed, claim: UUID) -> UUID:
    """Link a second drug to the slipped readout by hand, citing `claim`, with no `drugs` change row."""
    drug = Drug(company_id=seed.example, preferred_name="secondumab")
    _add(session, drug)
    _add(
        session, CatalystDrug(catalyst_id=seed.slipped, drug_id=drug.id, role=DrugRole.ALONE, claim_id=claim)
    )
    return drug.id


def test_a_hand_added_drug_link_never_leaks_an_unpublished_claim(
    session_factory: sessionmaker[Session], seed: Seed, caplog: pytest.LogCaptureFixture
) -> None:
    with session_factory.begin() as session:
        _second_drug(session, seed, seed.unpublished_claim)
    with caplog.at_level(logging.INFO, logger="catalyst"), read_session(session_factory) as session:
        pages = [
            catalyst(session, seed.slipped, Access.FREE),
            upcoming(session, Query(), Access.FREE, TODAY),
            upcoming(session, Query(drug="secondumab"), Access.FREE, TODAY),
            company_catalysts(session, "EXB", Query(), Access.PAID, TODAY),
        ]
    assert pages[0] is None
    for page in pages[1:]:
        assert page is not None
        assert seed.slipped not in _ids(page)
        assert UNPUBLISHED_QUOTE not in page.model_dump_json()
    assert any(r.getMessage() == "catalyst_withheld" for r in caplog.records)


def test_drug_links_must_match_the_latest_drugs_change(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with session_factory.begin() as session:
        second = _second_drug(session, seed, seed.unpublished_claim)
        published = TABLES[Claim].c
        first = session.execute(
            select(published.id).where(published.quote == "topline data in the third quarter of 2026")
        ).scalar_one()
        # A later drugs change that states both links, but the second link cites another claim.
        _add(
            session,
            CatalystChange(
                catalyst_id=seed.slipped,
                field=CatalystField.DRUGS,
                old_value=drugs_value((seed.drug, DrugRole.ALONE)),
                new_value=drugs_value((seed.drug, DrugRole.ALONE), (second, DrugRole.ALONE)),
                claim_id=first,
                learned_at=MOVED,
            ),
        )
    with read_session(session_factory) as session:
        assert catalyst(session, seed.slipped, Access.PAID) is None
    with session_factory.begin() as session:
        link = TABLES[CatalystDrug]
        session.execute(update(link).where(link.c.drug_id == second).values(claim_id=first))
    with read_session(session_factory) as session:
        found = catalyst(session, seed.slipped, Access.FREE)
    assert found is not None
    assert {d.name for d in found.catalyst.drugs} == {"exampumab", "secondumab"}
    assert UNPUBLISHED_QUOTE not in found.model_dump_json()


def test_a_drug_link_removed_by_hand_is_never_shown(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with session_factory.begin() as session:
        link = TABLES[CatalystDrug]
        session.execute(delete(link).where(link.c.catalyst_id == seed.slipped))
    with read_session(session_factory) as session:
        assert catalyst(session, seed.slipped, Access.PAID) is None


def test_a_drug_link_with_no_drugs_change_is_never_shown(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    with session_factory.begin() as session:
        claim = Claim(
            source_document_id=session.execute(
                select(TABLES[Claim].c.source_document_id).limit(1)
            ).scalar_one(),
            subject_type=ClaimSubject.CATALYST,
            field="c:8",
            value={"quote": "exampumab"},
            quote="exampumab",
            quote_start=0,
            quote_end=9,
            agent="extractor",
            prompt_id="extract_catalysts",
            prompt_version="1.0.0",
            provider="anthropic",
            model="m",
            extracted_at=FIRST,
        )
        _add(session, claim)
        _add(
            session,
            CatalystDrug(catalyst_id=seed.adcom, drug_id=seed.drug, role=DrugRole.ALONE, claim_id=claim.id),
        )
    with read_session(session_factory) as session:
        assert catalyst(session, seed.adcom, Access.PAID) is None


def _move_again(session: Session, seed: Seed, *, split: bool) -> None:
    """Move the slipped readout again, from 4Q26 to November 2026, citing a newer release.

    With `split`, the move's rows were written a minute apart, as one slow gate run might.
    """
    claims = TABLES[Claim].c
    source = session.execute(
        select(claims.source_document_id).where(claims.quote.like("topline data now expected%"))
    ).scalar_one()
    quote = "topline data now expected in November 2026"
    claim = Claim(
        source_document_id=source,
        subject_type=ClaimSubject.CATALYST,
        field="catalyst:9",
        value={"quote": quote},
        quote=quote,
        quote_start=0,
        quote_end=len(quote),
        agent="extractor",
        prompt_id="extract_catalysts",
        prompt_version="1.0.0",
        provider="anthropic",
        model="m",
        extracted_at=MOVED_AGAIN,
    )
    _add(session, claim)
    rows = [
        (CatalystField.WINDOW_START, "2026-10-01", "2026-11-01"),
        (CatalystField.WINDOW_END, "2026-12-31", "2026-11-30"),
        (CatalystField.WINDOW_PRECISION, "quarter", "month"),
        (CatalystField.WINDOW_RAW_TEXT, "4Q26", "November 2026"),
    ]
    for n, (field, old, new) in enumerate(rows):
        when = MOVED_AGAIN + timedelta(minutes=n if split else 0)
        _add(
            session,
            CatalystChange(
                catalyst_id=seed.slipped,
                field=field,
                old_value=old,
                new_value=new,
                claim_id=claim.id,
                learned_at=when,
            ),
        )
    table = TABLES[Catalyst]
    session.execute(
        update(table)
        .where(table.c.id == seed.slipped)
        .values(
            window_start=date(2026, 11, 1),
            window_end=date(2026, 11, 30),
            window_precision="month",
            window_raw_text="November 2026",
        )
    )


@pytest.mark.parametrize("split", [False, True])
def test_free_sees_only_the_rows_of_the_latest_move(
    session_factory: sessionmaker[Session], seed: Seed, split: bool
) -> None:
    with session_factory.begin() as session:
        _move_again(session, seed, split=split)
    with read_session(session_factory) as session:
        free = catalyst(session, seed.slipped, Access.FREE)
    assert free is not None
    latest = free.catalyst.latest_date_change
    assert {(c.field, c.old_value, c.new_value) for c in latest} == {
        ("window_start", "2026-10-01", "2026-11-01"),
        ("window_end", "2026-12-31", "2026-11-30"),
        ("window_precision", "quarter", "month"),
        ("window_raw_text", "4Q26", "November 2026"),
    }
    assert all(c.source.quote == "topline data now expected in November 2026" for c in latest)
    assert all(c.learned_at >= MOVED_AGAIN for c in latest)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2027, 1, 1), None),
        (date(2026, 11, 1), None),
        (None, date(2027, 6, 30)),
        (date(2027, 1, 1), date(2027, 6, 30)),
    ],
)
def test_a_free_page_never_reports_a_window_past_the_horizon(
    session_factory: sessionmaker[Session], seed: Seed, start: date | None, end: date | None
) -> None:
    with read_session(session_factory) as session:
        page = upcoming(session, Query(start=start, end=end), Access.FREE, TODAY)
    assert page.window_to == date(2026, 11, 16)
