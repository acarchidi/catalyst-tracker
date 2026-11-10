"""Matching an extracted indication to one MeSH disease, by plain code (ADR 0053, decision 2).

The fixture holds 14 whole descriptor records copied from NLM's 2026 MeSH file (desc2026.gz),
unchanged: 13 diseases and one drug (Aspirin), which must never match.
"""

import gzip
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.run import ON_REQUEST, SOURCES, build_clients, collect_all
from catalyst.collectors.sources import mesh as mesh_source
from catalyst.db.tables import TABLES
from catalyst.mesh import Descriptor, MeshIndex, Miss, normal_words
from catalyst.schemas import SourceDocument, SourceTier
from catalyst.snapshots import LocalSnapshotStore
from conftest import RecordedServer

FIXTURE = Path(__file__).parent / "fixtures" / "mesh" / "desc2026_excerpt.xml"


@pytest.fixture(scope="module")
def index() -> MeshIndex:
    return MeshIndex.from_xml(FIXTURE.read_bytes())


def test_only_diseases_are_indexed(index: MeshIndex) -> None:
    names = {descriptor.name for descriptor in index.descriptors}
    assert "Aspirin" not in names  # a drug (tree D), not a disease (trees C and F03)
    assert {"Melanoma", "Parkinson Disease", "Dermatitis, Atopic", "Heart Failure"} <= names
    assert len(names) == 13


def test_the_index_reads_a_gzip_copy_too() -> None:
    assert MeshIndex.from_xml(gzip.compress(FIXTURE.read_bytes())).descriptors


@pytest.mark.parametrize(
    ("text", "ui", "name"),
    [
        ("melanoma", "D008545", "Melanoma"),
        ("Melanoma", "D008545", "Melanoma"),
        ("atopic dermatitis", "D003876", "Dermatitis, Atopic"),  # an entry term in reading order
        ("Parkinson's disease", "D010300", "Parkinson Disease"),  # the possessive is dropped
        ("Parkinson\u2019s disease with pathogenic LRRK2 variant", "D010300", "Parkinson Disease"),
        ("relapsed/refractory acute myeloid leukemia (AML)", "D015470", "Leukemia, Myeloid, Acute"),
        ("heart failure with preserved ejection fraction", "D006333", "Heart Failure"),
        # "Carcinoma, Non-Small-Cell Lung" read in natural order, inside a longer text
        (
            "first-line EGFR exon 20 mutant non-small-cell lung carcinoma",
            "D002289",
            "Carcinoma, Non-Small-Cell Lung",
        ),
        ("non-small cell lung cancer", "D002289", "Carcinoma, Non-Small-Cell Lung"),  # a MeSH entry term
        ("metabolic dysfunction-associated steatohepatitis", "D065626", "Non-alcoholic Fatty Liver Disease"),
        (
            "moderate-to-severe nonalcoholic fatty liver disease",
            "D065626",
            "Non-alcoholic Fatty Liver Disease",
        ),
    ],
)
def test_one_disease_term_in_the_text_matches_its_descriptor(
    index: MeshIndex, text: str, ui: str, name: str
) -> None:
    found = index.match(text)
    assert not isinstance(found, Miss)
    assert found.descriptor == Descriptor(ui=ui, name=name)
    assert found.words  # the words of the text that matched, for the quote check


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", Miss.NO_DISEASE),
        ("advanced solid tumors", Miss.NO_DISEASE),
        ("aspirin", Miss.NO_DISEASE),  # a drug is not a disease
        ("MS", Miss.NO_DISEASE),  # a short abbreviation is never matched alone
        ("MASH", Miss.NO_DISEASE),  # not a MeSH term; no guessing
        ("liver cirrhosis with portal hypertension", Miss.SEVERAL_DISEASES),
        ("hypertension and heart failure", Miss.SEVERAL_DISEASES),
    ],
)
def test_no_disease_or_more_than_one_gives_no_match(index: MeshIndex, text: str, reason: Miss) -> None:
    assert index.match(text) is reason


def test_words_match_whole_words_only(index: MeshIndex) -> None:
    assert index.match("premelanoma") is Miss.NO_DISEASE
    assert index.match("melanoma-associated retinopathy") is Miss.NO_DISEASE  # a hyphenated word is one word


def test_normal_words_ignore_case_punctuation_brackets_and_possessives() -> None:
    assert normal_words("Parkinson\u2019s Disease (PD)") == ("parkinson", "disease")
    assert normal_words("Non-Small-Cell  Lung") == ("non-small-cell", "lung")
    assert normal_words("Dermatitis, Atopic") == ("dermatitis", "atopic")


@given(st.text(max_size=200))
def test_any_text_gives_a_match_or_a_miss_and_never_fails(text: str) -> None:
    found = MeshIndex.from_xml(FIXTURE.read_bytes()).match(text)
    assert isinstance(found, Miss) or found.descriptor.ui.startswith("D")


def test_the_collector_stores_the_file_under_its_year_and_hash() -> None:
    content = gzip.compress(FIXTURE.read_bytes())
    document = mesh_source.to_document(2026, content)
    assert document.source_key.startswith("mesh:desc2026:")
    assert len(document.source_key.removeprefix("mesh:desc2026:")) == 12
    assert document.url == "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc2026.gz"
    assert document.content == content
    assert document.source_tier is SourceTier.REGISTRY
    assert mesh_source.to_document(2026, content).source_key == document.source_key  # same file, same key


# --- The collector, run only when named ---

NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


def _collect_mesh(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> tuple[RecordedServer, list[tuple[str, int, int, str | None]]]:
    content = gzip.compress(FIXTURE.read_bytes())
    server = RecordedServer(overrides={mesh_source.FILE_URL.format(year=2026): (200, content)})
    stats = collect_all(
        [],
        build_clients("data@example.org", server.transport, rate_limits=False),
        LocalSnapshotStore(tmp_path),
        session_factory,
        date(2026, 11, 1),
        now=lambda: NOW,
        sources=frozenset({"mesh"}),
    )
    return server, [(s.source, s.created, s.existing, s.error) for s in stats]


def test_the_mesh_run_stores_the_years_file_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    server, stats = _collect_mesh(session_factory, tmp_path)
    assert stats == [("mesh", 1, 0, None)]
    assert server.requested == [mesh_source.FILE_URL.format(year=2026)]
    _, again = _collect_mesh(session_factory, tmp_path)
    assert again == [("mesh", 0, 1, None)]
    document = TABLES[SourceDocument]
    with session_factory() as session:
        keys = (
            session.execute(select(document.c.source_key).where(document.c.source_key.startswith("mesh:")))
            .scalars()
            .all()
        )
    assert len(keys) == 1
    assert keys[0].startswith("mesh:desc2026:")


def test_the_mesh_run_happens_only_when_named() -> None:
    assert "mesh" in ON_REQUEST
    assert "mesh" not in SOURCES


def test_the_stored_file_builds_the_index(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with session_factory() as session:
        assert mesh_source.stored_index(session, LocalSnapshotStore(tmp_path)) is None
    _collect_mesh(session_factory, tmp_path)
    with session_factory() as session:
        index = mesh_source.stored_index(session, LocalSnapshotStore(tmp_path))
    assert index is not None
    assert len(index.descriptors) == 13
