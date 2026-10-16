"""Gate 1 tools: a fixed choice of 50 companies, and a random sample of stored records to click."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.run import PilotCompany, load_companies
from catalyst.db.repository import link_company_document, save_source_document, upsert_company
from catalyst.gate1 import companies_toml, sample_markdown, sample_records, select_companies
from catalyst.schemas import DocumentMatch, SourceDocument, SourceTier

PILOT = [
    PilotCompany(ticker="VRTX", cik=875320, openfda_manufacturer="Vertex"),
    PilotCompany(ticker="INCY", cik=879169, ctgov_sponsors=frozenset({"Incyte Corporation"})),
]
COVERAGE = [PilotCompany(ticker=f"T{n}", cik=n, ctgov_sponsors=frozenset({f"Co {n}"})) for n in range(1, 31)]
COVERAGE.append(PilotCompany(ticker="INCY", cik=879169, ctgov_sponsors=frozenset({"Incyte Corp"})))


def test_selection_keeps_every_pilot_company_and_fills_up_from_the_coverage_list() -> None:
    chosen = select_companies(PILOT, COVERAGE, count=12, seed=2026)
    ciks = [c.cik for c in chosen]
    assert len(chosen) == 12
    assert len(set(ciks)) == 12
    assert {875320, 879169} <= set(ciks)
    assert ciks == sorted(ciks)
    incyte = next(c for c in chosen if c.cik == 879169)
    assert incyte.ctgov_sponsors == frozenset({"Incyte Corporation"})  # the hand-checked pilot entry wins


def test_selection_is_the_same_every_time_for_the_same_seed() -> None:
    first = select_companies(PILOT, COVERAGE, count=12, seed=2026)
    assert first == select_companies(PILOT, COVERAGE, count=12, seed=2026)
    assert first != select_companies(PILOT, COVERAGE, count=12, seed=1)


@pytest.mark.parametrize("count", [1, 40])
def test_impossible_counts_are_refused(count: int) -> None:
    with pytest.raises(ValueError, match="count"):
        select_companies(PILOT, COVERAGE, count=count, seed=2026)


def test_the_written_selection_is_a_list_the_collectors_read(tmp_path: Path) -> None:
    chosen = select_companies(PILOT, COVERAGE, count=5, seed=2026)
    path = tmp_path / "gate1.toml"
    path.write_text(companies_toml(chosen, "Gate 1 companies, seed 2026"), encoding="utf-8")
    assert load_companies(path) == chosen
    assert path.read_text().startswith("# Gate 1 companies, seed 2026")


def _add(session_factory: sessionmaker[Session], n: int, *, linked: bool) -> None:
    sha = f"{n:064x}"
    doc = SourceDocument(
        source_key=f"ctgov:NCT{n:08d}:2026-09-01",
        url=f"https://clinicaltrials.gov/study/NCT{n:08d}",
        publisher="ClinicalTrials.gov",
        source_tier=SourceTier.REGISTRY,
        document_type="Registry record",
        published_at=datetime(2026, 9, 1, 4, 0, tzinfo=UTC),
        retrieved_at=datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
        sha256=sha,
        snapshot_key=f"sha256/{sha[:2]}/{sha}",
        content_type="application/json",
    )
    with session_factory.begin() as session:
        saved, _ = save_source_document(session, doc)
        if linked:
            company = upsert_company(session, 875320, "Vertex Pharmaceuticals Inc", "2834")
            link_company_document(session, company.id, saved.id, DocumentMatch.CTGOV_LEAD_SPONSOR)


def test_the_sample_is_random_but_repeatable_and_shows_each_record_with_its_link(
    session_factory: sessionmaker[Session],
) -> None:
    for n in range(1, 21):
        _add(session_factory, n, linked=n % 2 == 0)
    first = sample_records(session_factory, count=10, seed=2026)
    assert len(first) == 10
    assert first == sample_records(session_factory, count=10, seed=2026)
    text = sample_markdown(first)
    assert text.count("](https://clinicaltrials.gov/study/NCT") == 10
    assert "Vertex Pharmaceuticals Inc" in text


def test_a_small_database_gives_every_record(session_factory: sessionmaker[Session]) -> None:
    _add(session_factory, 1, linked=False)
    assert len(sample_records(session_factory, count=10, seed=2026)) == 1
