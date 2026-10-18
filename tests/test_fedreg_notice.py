"""Reading FDA advisory committee meeting notices from the Federal Register in plain code (ADR 0018, part 3).

The notices are the recorded Federal Register answers in tests/fixtures/fedreg-2026-*, turned into
stored copies by the collector itself, so the reader sees exactly the bytes the live job stores.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.collectors.run import build_clients
from catalyst.collectors.sources import fedreg
from catalyst.snapshots import LocalSnapshotStore
from catalyst.verify.fedreg_notice import (
    Application,
    Evidence,
    MeetingNotice,
    NoticeError,
    notices_for,
    parse_meeting_dates,
    read_notice,
)
from conftest import FIXTURES, RecordedServer

EMAIL = "tests@example.com"
PAGE_TWO = FIXTURES / "fedreg-2026-paged" / "01_www.federalregister.gov_api_v1_documents"


def _collected() -> dict[str, FetchedDocument]:
    """Every notice the collector keeps from both recorded answers, by document number."""
    clients = build_clients(EMAIL, RecordedServer("fedreg-2026-paged").transport, rate_limits=False)
    paged = fedreg.collect_advisory_committee_notices(clients.web, date(2026, 1, 1), max_pages=2, per_page=10)
    clients = build_clients(EMAIL, RecordedServer("fedreg-2026-08").transport, rate_limits=False)
    august = fedreg.collect_advisory_committee_notices(clients.web, date(2026, 8, 1))
    return {d.source_key.removeprefix("fedreg:"): d for d in [*paged, *august]}


COLLECTED = _collected()


def _notice(number: str) -> MeetingNotice:
    return read_notice(COLLECTED[number].content)


def _recorded(number: str, path: Path = PAGE_TWO) -> dict[str, Any]:
    results: list[dict[str, Any]] = json.loads(path.read_bytes())["results"]
    return next(r for r in results if r["document_number"] == number)


def _values(notice: MeetingNotice) -> dict[str, object]:
    return {
        "committee": notice.committee.value if notice.committee else None,
        "topic": notice.topic.value if notice.topic else None,
        "meeting": (notice.meeting.value.start, notice.meeting.value.end) if notice.meeting else None,
        "products": [f.value for f in notice.products],
        "applications": [str(f.value) for f in notice.applications],
        "sponsors": [f.value for f in notice.sponsors],
        "amendment": notice.amendment is not None,
    }


def _evidence(notice: MeetingNotice) -> list[tuple[object, Evidence]]:
    facts: list[tuple[object, Evidence]] = []
    for fact in (notice.committee, notice.topic):
        if fact is not None:
            facts.append((fact.value, fact.evidence))
    facts += [(f.value, f.evidence) for f in [*notice.products, *notice.sponsors]]
    facts += [(f.value.number, f.evidence) for f in notice.applications]
    if notice.meeting is not None:
        facts.append((None, notice.meeting.evidence))
    if notice.amendment is not None:
        facts.append((None, notice.amendment))
    return facts


# --- What the collector stores ---------------------------------------------------------------------


def test_the_collector_keeps_six_meeting_notices_from_the_recordings() -> None:
    assert sorted(COLLECTED) == [
        "2026-15017",
        "2026-15149",
        "2026-16245",
        "2026-18349",
        "2026-18920",
        "2026-19700",
    ]


def test_a_stored_notice_holds_no_meeting_date_section() -> None:
    # The search API sends only its default fields; the DATES section is not among them. So no
    # recorded notice can give its meeting date yet, and the reader must say so, not guess.
    for fetched in COLLECTED.values():
        stored = json.loads(fetched.content)
        assert "dates" not in stored
        assert "body" not in stored


# --- Every recorded notice, field by field ---------------------------------------------------------


EXPECTED: dict[str, dict[str, object]] = {
    "2026-19700": {
        "committee": "Dermatologic and Ophthalmic Drugs Advisory Committee",
        "topic": "New Drug Application (NDA) 219694 for Atropine Sulfate Ophthalmic Solution, 0.01%",
        "meeting": None,
        "products": ["Atropine Sulfate Ophthalmic Solution, 0.01%"],
        "applications": ["NDA 219694"],
        "sponsors": [],
        "amendment": False,
    },
    "2026-18920": {
        "committee": "Microbiology Devices Panel of the Medical Devices Advisory Committee",
        "topic": "Pathogen-Agnostic Sequencing",
        "meeting": None,
        "products": [],
        "applications": [],
        "sponsors": [],
        "amendment": False,
    },
    "2026-18349": {
        "committee": "Vaccines and Related Biological Products Advisory Committee",
        "topic": "2027 Southern Hemisphere Influenza Virus Vaccines",
        "meeting": None,
        "products": [],
        "applications": [],
        "sponsors": [],
        "amendment": False,
    },
    "2026-16245": {
        "committee": "Molecular and Clinical Genetics Panel of the Medical Devices Advisory Committee",
        "topic": "GRAIL, Inc. Galleri",
        "meeting": None,
        "products": ["Galleri"],
        "applications": [],
        "sponsors": ["GRAIL, Inc."],
        "amendment": False,
    },
    "2026-15149": {
        "committee": "Pediatric Advisory Committee (PAC)",
        "topic": None,
        "meeting": None,
        "products": [],
        "applications": [],
        "sponsors": [],
        "amendment": False,
    },
    "2026-15017": {
        "committee": "Cellular, Tissue, and Gene Therapies Advisory Committee",
        "topic": "Biologics License Application (BLA) 125827, From Replimune, Inc. "
        "for Vusolimogene Oderparepvec",
        "meeting": None,
        "products": ["Vusolimogene Oderparepvec"],
        "applications": ["BLA 125827"],
        "sponsors": ["Replimune, Inc."],
        "amendment": True,
    },
}


@pytest.mark.parametrize("number", sorted(EXPECTED))
def test_each_recorded_notice_reads_as_expected(number: str) -> None:
    notice = _notice(number)
    assert notice.document_number == number
    assert _values(notice) == EXPECTED[number]


def test_an_amendment_the_collector_skips_still_reads() -> None:
    # 2026-15016's title says "Amendment of Notice", not "Notice of Meeting", so the collector
    # does not keep it today. The reader handles it all the same.
    notice = read_notice(fedreg.to_document(_recorded("2026-15016")).content)
    assert _values(notice) == {
        "committee": "Cellular, Tissue, and Gene Therapies Advisory Committee",
        "topic": "Biologics License Application (BLA) 125842 From Capricor, Inc. for Deramiocel "
        "(Human Allogeneic Cardiosphere-Derived Cells)",
        "meeting": None,
        "products": ["Deramiocel (Human Allogeneic Cardiosphere-Derived Cells)"],
        "applications": ["BLA 125842"],
        "sponsors": ["Capricor, Inc."],
        "amendment": True,
    }


def test_the_reader_keeps_the_link_between_drug_number_and_company() -> None:
    (subject,) = _notice("2026-15017").subjects
    assert subject.application is not None
    assert subject.application.value == Application("BLA", "125827")
    assert subject.product is not None
    assert subject.product.value == "Vusolimogene Oderparepvec"
    assert subject.sponsor is not None
    assert subject.sponsor.value == "Replimune, Inc."


def test_an_amendment_does_not_lend_the_old_meeting_date() -> None:
    # The search excerpt of 2026-15017 says the meeting "would be held on July 30, 2026", which is
    # the date the amendment changed. That is not the meeting date, so none is given.
    assert "would be held on July 30, 2026" in _recorded("2026-15017")["excerpts"]
    assert _notice("2026-15017").meeting is None


def test_what_each_recorded_notice_is_missing() -> None:
    assert {number: _notice(number).missing() for number in sorted(EXPECTED)} == {
        "2026-15017": ["meeting dates"],
        "2026-15149": ["meeting dates", "products", "applications", "sponsors"],
        "2026-16245": ["meeting dates", "applications"],
        "2026-18349": ["meeting dates", "products", "applications", "sponsors"],
        "2026-18920": ["meeting dates", "products", "applications", "sponsors"],
        "2026-19700": ["meeting dates", "sponsors"],
    }


# --- Provenance: every value comes with the sentence it came from ------------------------------------


@pytest.mark.parametrize("number", sorted(EXPECTED))
def test_every_value_comes_with_its_exact_sentence_and_offsets(number: str) -> None:
    notice = _notice(number)
    facts = _evidence(notice)
    assert facts
    for value, evidence in facts:
        assert notice.text[evidence.start : evidence.end] == evidence.quote
        if value is not None:
            assert str(value) in evidence.quote


def test_title_values_quote_the_whole_title() -> None:
    notice = _notice("2026-19700")
    assert notice.committee is not None
    title = json.loads(COLLECTED["2026-19700"].content)["title"]
    assert notice.committee.evidence == Evidence("title", 0, len(title), title)


def test_an_amendment_quotes_the_abstract_sentence_that_says_so() -> None:
    amendment = _notice("2026-15017").amendment
    assert amendment is not None
    assert amendment.field == "abstract"
    assert amendment.quote == (
        "The Food and Drug Administration (FDA) is announcing an amendment to the notice of meeting "
        "of the Cellular, Tissue, and Gene Therapies Advisory Committee (the Committee)."
    )


def test_the_notice_text_is_the_stored_fields_one_per_line() -> None:
    stored = json.loads(COLLECTED["2026-18349"].content)
    assert _notice("2026-18349").text == f"{stored['title']}\n{stored['abstract']}"


# --- A DATES section, when a stored copy has one ------------------------------------------------------


def _with_dates(number: str, dates: str) -> MeetingNotice:
    """Read a recorded notice plus a DATES section in the documents API's `dates` field.

    The wording is the standard FDA form; the date itself is made up for the test, not recorded.
    """
    stored = json.loads(COLLECTED[number].content)
    stored["dates"] = dates
    return read_notice(json.dumps(stored).encode())


def test_a_dates_section_gives_the_meeting_date_and_its_sentence() -> None:
    dates = "The meeting will be held on November 16, 2026, from 9 a.m. to 5 p.m. Eastern Time."
    notice = _with_dates("2026-19700", dates)
    assert notice.meeting is not None
    assert (notice.meeting.value.start, notice.meeting.value.end) == (date(2026, 11, 16), date(2026, 11, 16))
    assert notice.meeting.evidence.field == "dates"
    assert notice.meeting.evidence.quote == dates
    assert notice.text[notice.meeting.evidence.start : notice.meeting.evidence.end] == dates
    assert "meeting dates" not in notice.missing()


def test_a_two_day_meeting_reads_as_a_start_and_an_end() -> None:
    notice = _with_dates(
        "2026-18920",
        "The meeting will be held on December 8 and 9, 2026, from 8:30 a.m. to 5 p.m. Eastern Time. "
        "Written comments may be submitted until December 1, 2026.",
    )
    assert notice.meeting is not None
    assert (notice.meeting.value.start, notice.meeting.value.end) == (date(2026, 12, 8), date(2026, 12, 9))
    assert notice.meeting.evidence.quote.endswith("Eastern Time.")


def test_two_meeting_sentences_that_disagree_give_no_date() -> None:
    notice = _with_dates(
        "2026-18920",
        "The meeting will be held on December 8, 2026, from 9 a.m. to 5 p.m. "
        "The meeting will be held on December 15, 2026, from 9 a.m. to 5 p.m.",
    )
    assert notice.meeting is None


# --- Application, drug, and company sentences in the body style ---------------------------------------


def test_a_body_sentence_names_the_application_drug_and_company() -> None:
    # Written in the agenda style FDA notices use; not a recorded sentence.
    stored = json.loads(COLLECTED["2026-18920"].content)
    stored["abstract"] += (
        " The committee will discuss new drug application (NDA) 214985, for sotagliflozin tablets, "
        "submitted by Lexicon Pharmaceuticals, Inc., for the proposed indication of type 1 diabetes."
    )
    notice = read_notice(json.dumps(stored).encode())
    (subject,) = notice.subjects
    assert subject.application is not None
    assert subject.application.value == Application("NDA", "214985")
    assert subject.product is not None
    assert subject.product.value == "sotagliflozin tablets"
    assert subject.sponsor is not None
    assert subject.sponsor.value == "Lexicon Pharmaceuticals, Inc."
    assert subject.product.evidence.quote.startswith("The committee will discuss")


@pytest.mark.parametrize(
    ("sentence", "expected"),
    [
        ("The committee will discuss supplemental biologics license application (sBLA) 761034/S-012.",
         Application("sBLA", "761034/S-012")),
        ("The committee will discuss premarket approval application (PMA) P200010.",
         Application("PMA", "P200010")),
        ("The committee will discuss BLA 125827 and related matters.", Application("BLA", "125827")),
        ("The panel will discuss 510(k) K231234.", Application("510(k)", "K231234")),
    ],
)  # fmt: skip
def test_application_numbers_of_each_kind(sentence: str, expected: Application) -> None:
    stored = json.loads(COLLECTED["2026-18920"].content)
    stored["abstract"] = sentence
    notice = read_notice(json.dumps(stored).encode())
    assert [f.value for f in notice.applications] == [expected]
    assert notice.products == []


def test_a_notice_that_is_not_json_is_refused() -> None:
    with pytest.raises(NoticeError):
        read_notice(b"<html>not a notice</html>")
    with pytest.raises(NoticeError):
        read_notice(json.dumps({"title": "no number"}).encode())


# --- Meeting date phrases (property-based) ------------------------------------------------------------

_MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]  # fmt: skip
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _written(day: date, weekday: bool = False, year: bool = True) -> str:
    text = f"{_MONTH_NAMES[day.month - 1]} {day.day}"
    if year:
        text += f", {day.year}"
    return f"{_WEEKDAYS[day.weekday()]}, {text}" if weekday else text


def _phrase(start: date, end: date, style: int) -> str:
    """Write a meeting's days the ways FDA notices do."""
    same_month = (start.year, start.month) == (end.year, end.month)
    if start == end:
        return f"The meeting will be held on {_written(start, weekday=style % 2 == 0)}, from 9 a.m. to 5 p.m."
    month = _MONTH_NAMES[start.month - 1]
    if same_month and style == 0 and (end - start).days == 1:
        return f"The meeting will be held on {month} {start.day} and {end.day}, {end.year}."
    if same_month and style == 1:
        return f"The meeting will be held on {month} {start.day}\u2013{end.day}, {end.year}."
    if style == 2:
        return (
            f"The meeting will be held on {_written(start)}, from 9 a.m. to 5 p.m., "
            f"and {_written(end)}, from 9 a.m. to 3 p.m. Eastern Time."
        )
    if style == 3 and start.year == end.year:
        return f"The meeting will take place {_written(start, year=False)} through {_written(end)}."
    return f"The meeting will be held on {_written(start, True)}, through {_written(end, True)}."


@given(
    start=st.dates(min_value=date(2026, 1, 1), max_value=date(2030, 12, 31)),
    length=st.integers(min_value=0, max_value=3),
    style=st.integers(min_value=0, max_value=4),
)
def test_meeting_date_phrases_read_as_their_first_and_last_day(start: date, length: int, style: int) -> None:
    end = start + timedelta(days=length)
    assert parse_meeting_dates(_phrase(start, end, style)) == (start, end)


@given(day=st.dates(min_value=date(2026, 1, 1), max_value=date(2030, 12, 31)), shift=st.integers(1, 6))
def test_a_weekday_that_contradicts_the_date_gives_no_date(day: date, shift: int) -> None:
    wrong = _WEEKDAYS[(day.weekday() + shift) % 7]
    sentence = f"The meeting will be held on {wrong}, {_written(day)}, from 9 a.m. to 5 p.m."
    assert parse_meeting_dates(sentence) is None


@given(text=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
def test_text_without_a_month_name_gives_no_date(text: str) -> None:
    if not any(name[:3] in text for name in _MONTH_NAMES):
        assert parse_meeting_dates(text) is None


@pytest.mark.parametrize(
    "sentence",
    [
        "The meeting will be held on February 30, 2027, from 9 a.m. to 5 p.m.",  # no such day
        "The meeting will be held on November 16, from 9 a.m. to 5 p.m.",  # no year written
        "The meeting will be held on November 2, 2026, and November 30, 2026.",  # four weeks apart
        "The meeting will be held on December 8, 2026, instead of December 1, 2026.",  # a change
        "The meeting will be open to the public.",
    ],
)
def test_phrases_that_give_no_date(sentence: str) -> None:
    assert parse_meeting_dates(sentence) is None


def test_a_meeting_across_the_new_year() -> None:
    sentence = "The meeting will be held on December 31 and January 1, 2027."
    assert parse_meeting_dates(sentence) == (date(2026, 12, 31), date(2027, 1, 1))


# --- Notices whose meeting falls in a window, from the database ---------------------------------------


def _store(factory: sessionmaker[Session], store: LocalSnapshotStore, fetched: FetchedDocument) -> None:
    store_fetched(fetched, store, factory, now=lambda: datetime(2026, 10, 17, 12, tzinfo=UTC))


def _dated(number: str, dates: str) -> FetchedDocument:
    """Store a recorded notice with a DATES section added (standard wording, made-up date)."""
    stored = _recorded(
        number, FIXTURES / "fedreg-2026-08" / "00_www.federalregister.gov_api_v1_documents.json"
    )
    return fedreg.to_document({**stored, "dates": dates})


def test_notices_for_a_window_reads_stored_notices(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    for fetched in COLLECTED.values():
        if fetched.source_key not in ("fedreg:2026-19700", "fedreg:2026-18920"):
            _store(session_factory, store, fetched)
    _store(session_factory, store, _dated("2026-19700", "The meeting will be held on November 16, 2026."))
    _store(
        session_factory, store, _dated("2026-18920", "The meeting will be held on December 8 and 9, 2026.")
    )
    with session_factory() as session:
        november = notices_for(session, store, date(2026, 11, 1), date(2026, 11, 30))
        december_9 = notices_for(session, store, date(2026, 12, 9), date(2026, 12, 31))
        january = notices_for(session, store, date(2027, 1, 1), date(2027, 1, 31))
        august = notices_for(session, store, date(2026, 8, 1), date(2026, 8, 31))
    assert [n.notice.document_number for n in november.matching] == ["2026-19700"]
    assert [n.notice.document_number for n in december_9.matching] == ["2026-18920"]
    assert january.matching == []
    assert november.unreadable == []
    # The four stored notices with no readable date are listed apart, never matched by guess.
    assert sorted(n.notice.document_number for n in november.undated) == [
        "2026-15017",
        "2026-15149",
        "2026-16245",
        "2026-18349",
    ]
    # A notice published after the window ends cannot announce a meeting inside it.
    assert sorted(n.notice.document_number for n in august.undated) == [
        "2026-15017",
        "2026-15149",
        "2026-16245",
    ]
    assert november.matching[0].document.source_key == "fedreg:2026-19700"


def test_a_stored_copy_that_cannot_be_read_is_reported(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    store = LocalSnapshotStore(tmp_path)
    _store(session_factory, store, COLLECTED["2026-19700"])
    for copy in tmp_path.rglob("*"):
        if copy.is_file():
            copy.write_bytes(b"changed")
    with session_factory() as session:
        found = notices_for(session, store, date(2026, 11, 1), date(2026, 11, 30))
    assert found.unreadable == ["fedreg:2026-19700"]
    assert found.matching == []
    assert found.undated == []


def test_a_window_must_not_end_before_it_starts(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    with session_factory() as session, pytest.raises(ValueError, match="window"):
        notices_for(session, LocalSnapshotStore(tmp_path), date(2026, 12, 1), date(2026, 11, 1))
