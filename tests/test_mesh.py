"""Matching an extracted indication to one MeSH disease, by plain code (ADR 0053, decision 2).

The fixture holds 32 whole descriptor records copied unchanged from NLM's 2026 MeSH file
(desc2026.gz), gzip-compressed as NLM sends it. Besides ordinary diseases it holds the cases the
review of PR 123 found: the broad descriptors (Disease, Syndrome, Neoplasms, Infections, Rare
Diseases), look-alike diseases (small cell against non-small cell lung cancer, alcoholic against
non-alcoholic fatty liver), accented names (Sjogren, Meniere), a short form that is also a word
(DOES), and one drug (Aspirin), which must never match.
"""

import gzip
from datetime import UTC, date, datetime
from functools import cache
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.run import ON_REQUEST, SOURCES, build_clients, collect_all
from catalyst.collectors.sources import mesh as mesh_source
from catalyst.db.tables import TABLES
from catalyst.mesh import Descriptor, Match, MeshIndex, Miss, normal_words
from catalyst.schemas import SourceDocument
from catalyst.snapshots import LocalSnapshotStore

FIXTURE = Path(__file__).parent / "fixtures" / "mesh" / "desc2026_excerpt.xml.gz"
NOW = datetime(2026, 11, 10, 18, 0, tzinfo=UTC)


@cache
def _index() -> MeshIndex:
    return MeshIndex.from_xml(FIXTURE.read_bytes())


def _found(text: str) -> Descriptor | Miss:
    result = _index().match(text)
    return result if isinstance(result, Miss) else result.descriptor


def test_only_named_diseases_are_indexed() -> None:
    names = {descriptor.name for descriptor in _index().descriptors}
    assert {"Melanoma", "Parkinson Disease", "Dermatitis, Atopic", "Heart Failure", "Seizures"} <= names
    # A drug is not a disease; the broadest descriptors name no disease (top of tree C, or listed).
    assert not names & {"Aspirin", "Disease", "Syndrome", "Neoplasms", "Infections", "Rare Diseases"}


def test_the_index_reads_the_file_compressed_or_not() -> None:
    plain = MeshIndex.from_xml(gzip.decompress(FIXTURE.read_bytes()))
    assert plain.terms == _index().terms


@pytest.mark.parametrize(
    ("text", "ui", "name"),
    [
        ("melanoma", "D008545", "Melanoma"),
        ("Melanoma", "D008545", "Melanoma"),
        ("atopic dermatitis", "D003876", "Dermatitis, Atopic"),  # an inverted term in reading order
        ("moderate-to-severe atopic dermatitis", "D003876", "Dermatitis, Atopic"),  # grading words
        ("Parkinson's disease", "D010300", "Parkinson Disease"),  # the possessive is dropped
        ("Parkinson\u2019s disease with pathogenic LRRK2 variant", "D010300", "Parkinson Disease"),
        ("relapsed/refractory acute myeloid leukemia (AML)", "D015470", "Leukemia, Myeloid, Acute"),
        ("heart failure with preserved ejection fraction", "D006333", "Heart Failure"),
        ("non-small cell lung cancer", "D002289", "Carcinoma, Non-Small-Cell Lung"),
        ("non small cell lung cancer", "D002289", "Carcinoma, Non-Small-Cell Lung"),  # never small cell
        ("non-small-cell lung cancer", "D002289", "Carcinoma, Non-Small-Cell Lung"),
        ("small-cell lung cancer", "D055752", "Small Cell Lung Carcinoma"),
        ("non alcoholic steatohepatitis", "D065626", "Non-alcoholic Fatty Liver Disease"),  # never alcoholic
        ("non-alcoholic steatohepatitis", "D065626", "Non-alcoholic Fatty Liver Disease"),
        ("Sj\u00f6gren's syndrome", "D012859", "Sjogren's Syndrome"),  # accents dropped
        ("M\u00e9ni\u00e8re's disease", "D008575", "Meniere Disease"),
        ("HIV infection", "D015658", "HIV Infections"),
        ("migraine with aura", "D020325", "Migraine with Aura"),  # the longer term wins
        ("Pompe disease", "D006009", "Glycogen Storage Disease Type II"),
        ("multiple sclerosis", "D009103", "Multiple Sclerosis"),
        ("patients with melanoma", "D008545", "Melanoma"),  # words naming people may come first
        ("melanoma in adults", "D008545", "Melanoma"),
    ],
)
def test_one_disease_term_in_the_text_matches_its_descriptor(text: str, ui: str, name: str) -> None:
    assert _found(text) == Descriptor(ui=ui, name=name)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", Miss.NO_DISEASE),
        ("aspirin", Miss.NO_DISEASE),  # a drug is not a disease
        ("disease", Miss.NO_DISEASE),  # broad descriptors are not indexed
        ("Phelan-McDermid syndrome", Miss.NO_DISEASE),
        ("advanced solid tumors", Miss.NO_DISEASE),
        ("HIV-1 infection", Miss.NO_DISEASE),
        ("C. difficile infection", Miss.NO_DISEASE),
        ("MS", Miss.NO_DISEASE),  # MeSH tags it as a short form
        ("does", Miss.NO_DISEASE),  # "DOES (Disorders of Excessive Somnolence)" is never a term
        ("MASH", Miss.NO_DISEASE),  # not a MeSH term; no guessing
        ("premelanoma", Miss.NO_DISEASE),  # whole words only
        ("KCNT1+ epilepsy", Miss.NARROWER),  # a word that is not a grade narrows the disease
        ("focal onset seizures", Miss.NARROWER),
        ("without heart failure", Miss.NEGATED),
        ("not melanoma", Miss.NEGATED),
        ("patients without prior heart failure", Miss.NEGATED),  # the walk back finds the negation
        # Rezdiffra's label wording: an unknown word before joining words still narrows the term.
        ("noncirrhotic MASH with moderate to advanced liver fibrosis", Miss.NARROWER),
        ("MASH and liver fibrosis", Miss.NARROWER),
        ("agitation in Parkinson's disease", Miss.NARROWER),
        ("no evidence of melanoma", Miss.NARROWER),
        ("heart failure due to ATTR", Miss.CAUSED),  # a cause after the term narrows it too
        ("liver cirrhosis with portal hypertension", Miss.SEVERAL_DISEASES),
        ("hypertension and heart failure", Miss.SEVERAL_DISEASES),
    ],
)
def test_no_disease_a_narrower_one_a_negated_one_or_several_give_no_match(text: str, reason: Miss) -> None:
    assert _found(text) is reason


def test_a_match_gives_the_words_that_named_it_as_normal_words_reads_them() -> None:
    found = _index().match("Relapsed/refractory Acute Myeloid Leukemia (AML)")
    assert isinstance(found, Match)
    assert found.words == ("acute", "myeloid", "leukemia")
    assert set(found.words) <= set(normal_words("Relapsed/refractory Acute Myeloid Leukemia (AML)"))


def test_normal_words_drop_case_accents_brackets_possessives_and_join_non() -> None:
    assert normal_words("Parkinson\u2019s Disease (PD)") == ("parkinson", "disease")
    assert normal_words("Non-Small-Cell  Lung") == ("nonsmall", "cell", "lung")
    assert normal_words("non small cell") == ("nonsmall", "cell")
    assert normal_words("Sj\u00f6gren") == ("sjogren",)
    assert normal_words("\u03b11-antitrypsin") == ("alpha", "1", "antitrypsin")  # Greek letters spelled out
    assert normal_words("Dermatitis, Atopic") == ("dermatitis", "atopic")


@given(st.text(max_size=200))
def test_any_text_gives_one_indexed_disease_or_a_miss_and_never_fails(text: str) -> None:
    found = _index().match(text)
    assert isinstance(found, Miss) or found.descriptor in _index().descriptors


# --- The collector, run only when named ---


class _Nlm:
    """NLM's file server as it answers: the .gz file labelled "Content-Encoding: x-gzip"."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.requested: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requested.append(url)
        if url in self.files:
            headers = {"content-encoding": "x-gzip", "content-type": "text/plain"}
            return httpx.Response(200, stream=httpx.ByteStream(self.files[url]), headers=headers)
        return httpx.Response(302, headers={"location": "https://www.nlm.nih.gov/cgi/archive/error.cgi"})


def _collect_mesh(
    session_factory: sessionmaker[Session], tmp_path: Path, files: dict[str, bytes]
) -> tuple[_Nlm, list[tuple[str, int, int, str | None]]]:
    server = _Nlm(files)
    stats = collect_all(
        [],
        build_clients("data@example.org", httpx.MockTransport(server), rate_limits=False),
        LocalSnapshotStore(tmp_path),
        session_factory,
        date(2026, 11, 1),
        now=lambda: NOW,
        sources=frozenset({"mesh"}),
    )
    return server, [(s.source, s.created, s.existing, s.error) for s in stats]


def _stored(session_factory: sessionmaker[Session], tmp_path: Path) -> tuple[list[str], bytes]:
    document = TABLES[SourceDocument]
    query = select(document.c.source_key, document.c.snapshot_key).where(
        document.c.source_key.startswith("mesh:")
    )
    with session_factory() as session:
        rows = session.execute(query).all()
    content = LocalSnapshotStore(tmp_path).get(rows[0].snapshot_key) if rows else b""
    return [row.source_key for row in rows], content


def test_the_mesh_run_stores_the_file_exactly_as_sent_once(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    sent = FIXTURE.read_bytes()
    this_year = mesh_source.FILE_URL.format(year=2026)
    server, stats = _collect_mesh(session_factory, tmp_path, {this_year: sent})
    assert stats == [("mesh", 1, 0, None)]
    # Next year's file is asked for first; NLM redirects a missing year to an error page.
    assert server.requested == [mesh_source.FILE_URL.format(year=2027), this_year]
    keys, content = _stored(session_factory, tmp_path)
    assert len(keys) == 1
    assert keys[0].startswith("mesh:desc2026:")
    assert content == sent  # the .gz file itself, never its unpacked text
    _, again = _collect_mesh(session_factory, tmp_path, {this_year: sent})
    assert again == [("mesh", 0, 1, None)]


def test_next_years_file_is_taken_once_nlm_posts_it(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    next_year = mesh_source.FILE_URL.format(year=2027)
    _, stats = _collect_mesh(session_factory, tmp_path, {next_year: FIXTURE.read_bytes()})
    assert stats == [("mesh", 1, 0, None)]
    keys, _ = _stored(session_factory, tmp_path)
    assert keys[0].startswith("mesh:desc2027:")


def test_a_reply_that_is_not_a_gzip_file_is_never_stored(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    error_page = b"<html>error</html>"
    files = {mesh_source.FILE_URL.format(year=year): error_page for year in (2026, 2027)}
    _, stats = _collect_mesh(session_factory, tmp_path, files)
    assert stats[0][:3] == ("mesh", 0, 0)
    assert stats[0][3] is not None
    assert _stored(session_factory, tmp_path)[0] == []


def test_the_stored_file_builds_the_index(session_factory: sessionmaker[Session], tmp_path: Path) -> None:
    with session_factory() as session:
        assert mesh_source.stored_index(session, LocalSnapshotStore(tmp_path)) is None
    _collect_mesh(session_factory, tmp_path, {mesh_source.FILE_URL.format(year=2026): FIXTURE.read_bytes()})
    with session_factory() as session:
        index = mesh_source.stored_index(session, LocalSnapshotStore(tmp_path))
    assert index is not None
    assert index.terms == _index().terms


def test_the_mesh_run_happens_only_when_named() -> None:
    assert "mesh" in ON_REQUEST
    assert "mesh" not in SOURCES


def test_the_as_sent_fetch_keeps_to_the_listed_hosts_and_the_size_cap() -> None:
    web = build_clients("data@example.org", httpx.MockTransport(_Nlm({})), rate_limits=False).web
    with pytest.raises(ValueError, match="only fetches"):
        web.get_as_sent("https://example.org/desc2026.gz", mesh_source.MAX_BYTES)
    big = {mesh_source.FILE_URL.format(year=2026): b"\x1f\x8b" + b"0" * 100}
    web = build_clients("data@example.org", httpx.MockTransport(_Nlm(big)), rate_limits=False).web
    with pytest.raises(ValueError, match="larger than"):
        web.get_as_sent(mesh_source.FILE_URL.format(year=2026), 50)


def test_a_timeout_on_next_years_file_still_takes_this_years(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    this_year = mesh_source.FILE_URL.format(year=2026)
    nlm = _Nlm({this_year: FIXTURE.read_bytes()})

    def answer(request: httpx.Request) -> httpx.Response:
        if str(request.url) == mesh_source.FILE_URL.format(year=2027):
            raise httpx.ConnectTimeout("timed out", request=request)
        return nlm(request)

    stats = collect_all(
        [],
        build_clients("data@example.org", httpx.MockTransport(answer), rate_limits=False),
        LocalSnapshotStore(tmp_path),
        session_factory,
        date(2026, 11, 1),
        now=lambda: NOW,
        sources=frozenset({"mesh"}),
    )
    assert [(x.source, x.created, x.error) for x in stats] == [("mesh", 1, None)]
