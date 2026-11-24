"""Company names as readers see them (ADR 0068).

The display name is the company's own spelling of its name in its own stored press releases,
matched to the SEC name case-insensitively after the resolver's normalization; without one it is
the SEC name with EDGAR's state suffix removed. It is never title-cased.
"""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.read import company_views
from catalyst.api.views import CompanyView
from catalyst.collectors.coverage import normalize_name
from catalyst.company_names import (
    display_name,
    own_spelling,
    recompute_display_names,
    without_state_suffix,
)
from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.db.tables import TABLES
from catalyst.references import CompanyIdentity, record_company_identity
from catalyst.schemas import Company, DocumentMatch, SourceDocument, SourceTier
from catalyst.snapshots import LocalSnapshotStore, Snapshot, SnapshotStore, SnapshotStoreError
from conftest import FIXTURES

VERTEX_RELEASE = (
    FIXTURES / "sec" / "875320" / "Archives_edgar_data_875320_000087532026000256_ex-991_q22026.htm"
)
NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
PUBLISHED = datetime(2026, 11, 3, 21, 5, tzinfo=UTC)

MACROGENICS = (
    "MACROGENICS PROVIDES UPDATE ON CORPORATE PROGRESS\n"
    "ROCKVILLE, MD, Nov. 4, 2026 -- MacroGenics, Inc. (NASDAQ: MGNX), a biopharmaceutical company, "
    "today reported results. MacroGenics expects data in 2027.\n"
    "About MacroGenics, Inc.\nMacroGenics is a biopharmaceutical company."
)


# The SEC name without its state suffix


@pytest.mark.parametrize(
    ("sec_name", "plain"),
    [
        ("Viridian Therapeutics, Inc.\\DE", "Viridian Therapeutics, Inc."),
        ("VERTEX PHARMACEUTICALS INC / MA", "VERTEX PHARMACEUTICALS INC"),
        ("Foo Therapeutics Inc /DE/", "Foo Therapeutics Inc"),
        ("Bar Biosciences Corp/MD", "Bar Biosciences Corp"),
        ("Dave Inc./DE", "Dave Inc."),
        ("PALISADE BIO, INC.", "PALISADE BIO, INC."),
        ("Genmab A/S", "Genmab A/S"),
        ("MACROGENICS INC", "MACROGENICS INC"),  # never title-cased
    ],
)
def test_the_state_suffix_is_removed_and_nothing_else(sec_name: str, plain: str) -> None:
    assert without_state_suffix(sec_name) == plain


# The company's own spelling


def test_the_release_spelling_wins_over_the_capitalised_sec_name() -> None:
    assert own_spelling("MACROGENICS INC", [MACROGENICS]) == "MacroGenics, Inc."


def test_a_headline_in_capitals_is_not_a_spelling() -> None:
    text = "PALISADE BIO ANNOUNCES TOPLINE DATA\nPALISADE BIO, INC. (NASDAQ: PALI)"
    assert own_spelling("PALISADE BIO, INC.", [text]) is None
    assert display_name("PALISADE BIO, INC.", [text]) == "PALISADE BIO, INC."


def test_the_full_legal_name_is_preferred_and_a_sentence_end_is_not_kept() -> None:
    text = (
        "WILMINGTON, Del. -- Incyte (Nasdaq:INCY) today announced results. Incyte will host a call. "
        "The shares are held by Incyte Corporation."
    )
    assert own_spelling("INCYTE CORP", [text]) == "Incyte Corporation"


def test_a_short_name_alone_is_used_when_no_legal_form_is_written() -> None:
    assert own_spelling("INCYTE CORP", ["Incyte (Nasdaq:INCY) today announced results."]) == "Incyte"


def test_a_dotted_legal_form_is_kept() -> None:
    text = "NAARDEN, the Netherlands -- NewAmsterdam Pharma Company N.V. (Nasdaq: NAMS) today announced"
    assert own_spelling("NewAmsterdam Pharma Co N.V.", [text]) == "NewAmsterdam Pharma Company N.V."


def test_another_companys_name_is_not_a_spelling() -> None:
    text = "Partner Bio, Inc. and Vertex today announced a collaboration."
    assert own_spelling("MACROGENICS INC", [text]) is None


def test_the_most_common_spelling_across_releases_wins() -> None:
    older = "Palisade Bio, Inc. (Nasdaq: PALI) today announced"
    newer = "Palisade Bio, Inc. today said. Palisade BIO, Inc. once."
    assert own_spelling("PALISADE BIO, INC.", [newer, older]) == "Palisade Bio, Inc."


def test_the_recorded_vertex_release_gives_vertexs_own_spelling() -> None:
    text = _text(VERTEX_RELEASE.read_bytes(), "text/html")
    assert own_spelling("VERTEX PHARMACEUTICALS INC / MA", [text]) == "Vertex Pharmaceuticals Incorporated"


def test_without_a_release_the_sec_name_loses_only_its_suffix() -> None:
    assert display_name("Viridian Therapeutics, Inc.\\DE", []) == "Viridian Therapeutics, Inc."
    assert display_name("MACROGENICS INC", []) == "MACROGENICS INC"


@given(st.text(max_size=300))
def test_a_display_name_always_names_the_same_company(text: str) -> None:
    for sec_name in ("MACROGENICS INC", "Viridian Therapeutics, Inc.\\DE", "NewAmsterdam Pharma Co N.V."):
        name = display_name(sec_name, [text, MACROGENICS])
        assert normalize_name(name) == normalize_name(sec_name)


# The stored names and the read layer


class _BrokenStore:
    """A store whose copies cannot be read."""

    def put(self, content: bytes) -> Snapshot:
        raise SnapshotStoreError("read only")

    def get(self, key: str) -> bytes:
        raise SnapshotStoreError(f"cannot read {key}")


@pytest.fixture
def store(tmp_path: Path) -> LocalSnapshotStore:
    return LocalSnapshotStore(tmp_path / "snapshots")


def _text(content: bytes, content_type: str) -> str:
    from catalyst.extract.text import document_text

    return document_text(content, content_type)


def _company(session: Session, cik: int, name: str, tickers: list[tuple[str, str | None]]) -> UUID:
    company = upsert_company(session, cik, name, "2834").id
    record_company_identity(
        session,
        company,
        CompanyIdentity(name=name, former_names=(), tickers=tuple(tickers)),
        date(2026, 11, 1),
    )
    return company


def _release(
    session: Session,
    store: SnapshotStore,
    company: UUID,
    key: str,
    content: bytes,
    *,
    document_type: str = "8-K EX-99.1",
    matched_by: DocumentMatch = DocumentMatch.SEC_CIK,
    published: datetime = PUBLISHED,
) -> UUID:
    snapshot = store.put(content)
    document, _ = save_source_document(
        session,
        SourceDocument(
            source_key=key,
            url=f"https://www.sec.gov/{key}.htm",
            publisher="SEC EDGAR",
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type=document_type,
            published_at=published,
            retrieved_at=published,
            sha256=snapshot.sha256,
            snapshot_key=snapshot.key,
            content_type="text/html",
        ),
    )
    link_company_document(session, company, document.id, matched_by)
    return document.id


def _stored(session_factory: sessionmaker[Session], company: UUID) -> tuple[str | None, UUID | None]:
    table = TABLES[Company]
    with session_factory() as session:
        row = session.execute(
            select(table.c.display_name, table.c.display_name_document_id).where(table.c.id == company)
        ).one()
        return row.display_name, row.display_name_document_id


def test_the_job_stores_the_release_spelling_with_its_document(
    session_factory: sessionmaker[Session], store: LocalSnapshotStore
) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1125345, "MACROGENICS INC", [("MGNX", "Nasdaq")])
        release = _release(session, store, company, "sec:mgnx:1", f"<p>{MACROGENICS}</p>".encode())
    report = recompute_display_names(session_factory, store)
    assert (report.companies, report.changed, report.from_documents) == (1, 1, 1)
    assert _stored(session_factory, company) == ("MacroGenics, Inc.", release)


def test_a_rerun_with_nothing_new_reads_and_writes_nothing(
    session_factory: sessionmaker[Session], store: LocalSnapshotStore
) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1125345, "MACROGENICS INC", [("MGNX", "Nasdaq")])
        _release(session, store, company, "sec:mgnx:1", f"<p>{MACROGENICS}</p>".encode())
    recompute_display_names(session_factory, store)
    again = recompute_display_names(session_factory, _BrokenStore())
    assert (again.companies, again.unchanged, again.changed, again.documents_read) == (1, 1, 0, 0)
    assert _stored(session_factory, company)[0] == "MacroGenics, Inc."


def test_only_the_companys_own_press_releases_count(
    session_factory: sessionmaker[Session], store: LocalSnapshotStore
) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1590750, "Viridian Therapeutics, Inc.\\DE", [("VRDN", "Nasdaq")])
        spelled = b"<p>VIRIDIAN therapeutics, inc. is spelled oddly here.</p>"
        _release(session, store, company, "ctgov:NCT1", spelled, matched_by=DocumentMatch.CTGOV_LEAD_SPONSOR)
        _release(session, store, company, "sec:vrdn:8k", spelled, document_type="8-K")
    recompute_display_names(session_factory, store)
    assert _stored(session_factory, company) == ("Viridian Therapeutics, Inc.", None)


def test_a_copy_that_cannot_be_read_is_tried_again_next_run(
    session_factory: sessionmaker[Session], store: LocalSnapshotStore
) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1125345, "MACROGENICS INC", [("MGNX", "Nasdaq")])
        _release(session, store, company, "sec:mgnx:1", f"<p>{MACROGENICS}</p>".encode())
    failed = recompute_display_names(session_factory, _BrokenStore())
    assert (failed.documents_unreadable, _stored(session_factory, company)[0]) == (1, "MACROGENICS INC")
    recompute_display_names(session_factory, store)
    assert _stored(session_factory, company)[0] == "MacroGenics, Inc."


def test_a_new_sec_name_is_noticed(session_factory: sessionmaker[Session], store: LocalSnapshotStore) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1590750, "Viridian Therapeutics, Inc.\\DE", [("VRDN", "Nasdaq")])
    recompute_display_names(session_factory, store)
    with session_factory.begin() as session:
        table = TABLES[Company]
        session.execute(update(table).where(table.c.id == company).values(name="Viridian Bio, Inc.\\DE"))
    report = recompute_display_names(session_factory, store)
    assert report.changed == 1
    assert _stored(session_factory, company)[0] == "Viridian Bio, Inc."


def test_only_the_newest_releases_are_read(
    session_factory: sessionmaker[Session], store: LocalSnapshotStore
) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1125345, "MACROGENICS INC", [("MGNX", "Nasdaq")])
        old = b"<p>Macrogenics, Inc. once wrote its name like this, many times. Macrogenics, Inc.</p>"
        _release(session, store, company, "sec:mgnx:old", old, published=PUBLISHED - timedelta(days=400))
        for day in range(3):
            body = f"<p>Day {day}. {MACROGENICS}</p>".encode()
            _release(
                session, store, company, f"sec:mgnx:{day}", body, published=PUBLISHED + timedelta(days=day)
            )
    report = recompute_display_names(session_factory, store)
    assert report.documents_read == 3
    assert _stored(session_factory, company)[0] == "MacroGenics, Inc."


def test_the_read_layer_shows_the_display_name_beside_the_sec_name(
    session_factory: sessionmaker[Session], store: LocalSnapshotStore
) -> None:
    with session_factory.begin() as session:
        kiora = _company(session, 1372514, "KIORA PHARMACEUTICALS INC", [("KPRX", "Nasdaq")])
        text = b"<p>Kiora Pharmaceuticals, Inc. (NASDAQ: KPRX) today announced</p>"
        _release(session, store, kiora, "sec:kprx:1", text)
    recompute_display_names(session_factory, store)
    with session_factory() as session:
        view = company_views(session, [kiora])[kiora]
    assert (view.name, view.display_name, view.ticker) == (
        "KIORA PHARMACEUTICALS INC",
        "Kiora Pharmaceuticals, Inc.",
        "KPRX",
    )


def test_a_company_not_yet_computed_shows_the_plain_sec_name(session_factory: sessionmaker[Session]) -> None:
    with session_factory.begin() as session:
        company = _company(session, 1590750, "Viridian Therapeutics, Inc.\\DE", [("VRDN", "Nasdaq")])
    with session_factory() as session:
        view = company_views(session, [company])[company]
    assert view.display_name == "Viridian Therapeutics, Inc."


def test_an_older_export_without_display_names_still_reads() -> None:
    view = CompanyView.model_validate(
        {"id": str(UUID(int=1)), "name": "MACROGENICS INC", "cik": 1, "ticker": None}
    )
    assert view.display_name == "MACROGENICS INC"
