"""The read layer shows only published values, each with its source and quote (spec: Provenance)."""

from collections.abc import Iterator
from datetime import date
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, event, insert, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import MOVED, TODAY, UNPUBLISHED_QUOTE, Seed
from catalyst.api.access import Access
from catalyst.api.read import Query, catalyst, company_catalysts, read_session, upcoming
from catalyst.api.views import CatalystDetail
from catalyst.db.tables import TABLES
from catalyst.schemas import Catalyst, CatalystType, Company


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
    assert len(paid.catalyst.history) == 9 + 4  # nine first values, four moved


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
        for table in ("claim_check", "entity_match", "document_reading", "model_call"):
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
