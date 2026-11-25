"""Duplicate entries (ADR 0082): two open entries that are one event become one.

The weekly issue sometimes listed one real event twice for a company: the same drug's conference
presentation, or the same readout from two releases that named the drug two ways. The rule is
plain code in the nightly write step. Only when every test holds does it keep one entry and mark
the other `merged`, with a status change in its history; the merged entry's claims are then shown
as extra sources of the kept one. When unsure, it does nothing.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.access import Access
from catalyst.api.read import SAME_EVENT, Query, published, upcoming
from catalyst.api.read import catalyst as read_catalyst
from catalyst.db.tables import TABLES
from catalyst.export import build_export
from catalyst.outreach.digest import build_digest
from catalyst.publish.closed_deals import CLOSED_EVENT, DEAL_CLOSER
from catalyst.publish.duplicates import (
    DEDUPLICATOR,
    Entry,
    meeting_names,
    merge_plan,
    normal_title,
    same_event,
    title_marks,
)
from catalyst.publish.live import MERGED_EVENT, Rank
from catalyst.publish.withdrawals import Withdrawal
from catalyst.publish.write import WITHDRAWN_EVENT, publish_claims
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    CatalystField,
    CatalystMergedSource,
    Claim,
    ClaimCheck,
    ClaimSubject,
    Drug,
    DrugName,
    DrugNameKind,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore
from test_publish_writer import NOW, OCT_14, OCT_16, VERIFIER_MODEL, World

COMPANY = UUID(int=1)
OTHER_COMPANY = UUID(int=2)
TRIAL = UUID(int=10)
OTHER_TRIAL = UUID(int=11)
SEC, WIRE = SourceTier.REGULATOR_OR_SEC, SourceTier.WIRE
OLDER, NEWER = OCT_14, OCT_16


TITLE = "Phase 3 topline data for pegozafermin in MASH"


def _entry(**changes: Any) -> Entry:
    """Return a Phase 3 topline readout of pegozafermin in MASH, from an SEC filing, unless changed.

    A `title` is the extractor's own (never published); its marks are read from it unless given.
    """
    title = changes.pop("title", TITLE)
    fields: dict[str, Any] = {
        "title": normal_title(title),
        "marks": title_marks(title),
        "id": uuid4(),
        "company_id": COMPANY,
        "catalyst_type": "topline_readout",
        "drug": "pegozafermin",
        "roles": frozenset({"alone"}),
        "trials": frozenset({TRIAL}),
        "indication": "mash",
        "indication_id": None,
        "window": (date(2027, 7, 1), date(2027, 12, 31)),
        "meetings": frozenset[str](),
        "rank": Rank(SEC, OLDER),
    }
    return Entry(**(fields | changes))


# --- The rule: every test must hold ---


_SAME: list[tuple[dict[str, Any], dict[str, Any]]] = [
    ({}, {"rank": Rank(SEC, NEWER)}),
    ({}, {"drug": "pegozafermin (BIO89-100)"}),  # "NAME (OTHER)" is one drug (ADR 0062)
    ({}, {"drug": "Pegozafermin injection"}),  # form words and case do not matter
    ({}, {"trials": frozenset[UUID]()}),  # one side names no trial
    ({}, {"indication": ""}),  # one side names no indication
    ({}, {"window": (date(2027, 10, 1), date(2027, 12, 31))}),  # the windows overlap
    ({}, {"roles": frozenset[str]()}),  # one side has no drug link on file
    ({"drug": "X plus Y"}, {"drug": "X in combination with Y"}),
]


@pytest.mark.parametrize(("first", "second"), _SAME)
def test_two_entries_of_one_event_are_the_same(first: dict[str, Any], second: dict[str, Any]) -> None:
    a, b = _entry(**first), _entry(**second)
    assert same_event(a, b)
    assert same_event(b, a)


@pytest.mark.parametrize(
    "change",
    [
        {"company_id": OTHER_COMPANY},
        {"catalyst_type": "pdufa_date"},
        {"indication": "nsclc"},  # different indications
        {"indication_id": UUID(int=99)},  # different matched indications, both known
        {"drug": "pegozafermin with semaglutide"},  # a combination is never the drug alone
        {"drug": "pegozafermin plus semaglutide"},
        {"drug": "efruxifermin"},
        {"drug": None},  # no drug: plain code cannot tell two such events apart
        {"roles": frozenset({"in_combination"})},
        {"trials": frozenset({OTHER_TRIAL})},  # different trials
        {"trials": frozenset({TRIAL, OTHER_TRIAL})},
        {"window": (date(2028, 1, 1), date(2028, 6, 30))},  # windows that do not overlap
        {"title": "Phase 2 topline data for pegozafermin in MASH"},  # another phase
        {"title": "Phase 3 interim data for pegozafermin in MASH"},  # interim is not topline
        {"title": "Phase 3 final data for pegozafermin in MASH"},
        {"title": "Phase 3 topline data for pegozafermin in MASH in Japan"},  # a region of its own
    ],
)
def test_a_near_miss_is_never_the_same(change: dict[str, Any]) -> None:
    a = _entry(indication_id=UUID(int=98)) if "indication_id" in change else _entry()
    b = _entry(**change)
    assert not same_event(a, b)
    assert not same_event(b, a)


def test_a_phase_2_readout_without_a_trial_is_not_the_phase_3_trial_s_readout() -> None:
    """Opus review of 66ecb0f, finding 1: "Phase 2 topline 2H26" (no trial), a Phase 3 trial's "2026"."""
    phase_2 = _entry(
        title="Phase 2 topline data for pegozafermin expected in 2H26",
        trials=frozenset[UUID](),
        window=(date(2026, 7, 1), date(2026, 12, 31)),
    )
    phase_3 = _entry(
        title="Phase 3 RAINIER topline data for pegozafermin in 2026",
        window=(date(2026, 1, 1), date(2026, 12, 31)),
    )
    assert not same_event(phase_2, phase_3)
    # One side naming a trial and the other none needs the very same title, not only the same marks.
    reworded = _entry(title="Phase 3 topline results for pegozafermin in MASH", trials=frozenset[UUID]())
    assert not same_event(_entry(), reworded)
    assert same_event(_entry(), _entry(trials=frozenset[UUID]()))


def test_a_us_submission_is_not_a_european_one() -> None:
    """Opus review of 66ecb0f, finding 1: "BLA to FDA 2H26" and "MAA to EMA 2H26"."""
    kind: dict[str, Any] = {"catalyst_type": "regulatory_submission", "trials": frozenset[UUID]()}
    window = (date(2026, 7, 1), date(2026, 12, 31))
    bla = _entry(**kind, title="BLA submission to the FDA for pegozafermin in 2H26", window=window)
    maa = _entry(**kind, title="MAA submission to the EMA for pegozafermin in 2H26", window=window)
    assert title_marks("BLA submission to the FDA") == {"region:us"}
    assert title_marks("MAA submission to the EMA") == {"region:eu"}
    assert not same_event(bla, maa)
    unnamed = _entry(**kind, title="Regulatory submission for pegozafermin in 2H26", window=window)
    assert not same_event(bla, unnamed)  # one side names a regulator, the other none: unsure


@pytest.mark.parametrize(
    ("title", "marks"),
    [
        ("Phase 3 RAINIER topline data", {"phase:3", "stage:topline"}),
        ("Phase III top-line results", {"phase:3", "stage:topline"}),
        ("Phase 1/2 interim data", {"phase:1/2", "stage:interim"}),
        ("Phase IIb final analysis", {"phase:2b", "stage:final"}),
        ("sNDA filing with the U.S. FDA", {"region:us"}),
        ("CHMP opinion in the European Union", {"region:eu"}),
        ("PMDA submission in Japan", {"region:jp"}),
        ("NMPA acceptance in China", {"region:cn"}),
        ("Data for pegozafermin", set[str]()),
    ],
)
def test_title_marks_read_the_phase_the_stage_and_the_region(title: str, marks: set[str]) -> None:
    assert title_marks(title) == marks


def test_two_entries_without_a_drug_are_never_the_same() -> None:
    deal: dict[str, Any] = {
        "catalyst_type": "merger_acquisition",
        "drug": None,
        "roles": frozenset[str](),
        "trials": frozenset(),
    }
    assert not same_event(_entry(**deal), _entry(**deal))


CONFERENCE = {"catalyst_type": "conference_presentation", "trials": frozenset[UUID]()}
AASLD = frozenset({"aasld liver meeting"})
EASL = frozenset({"easl congress"})


def test_a_conference_needs_the_same_meeting_or_the_same_exact_dates() -> None:
    a = _entry(**CONFERENCE, meetings=AASLD, window=(date(2027, 7, 1), date(2027, 12, 31)))
    b = _entry(**CONFERENCE, meetings=AASLD, window=(date(2027, 11, 1), date(2027, 11, 30)))
    assert same_event(a, b)
    assert not same_event(a, _entry(**CONFERENCE, meetings=EASL, window=b.window))  # different meetings
    assert not same_event(a, _entry(**CONFERENCE, window=b.window))  # no meeting, other dates
    assert same_event(
        _entry(**CONFERENCE, window=b.window), _entry(**CONFERENCE, meetings=EASL, window=b.window)
    )
    two = _entry(**CONFERENCE, meetings=AASLD | EASL, window=(date(2027, 10, 1), date(2027, 11, 30)))
    assert not same_event(b, two)  # an entry naming two meetings names none for sure
    same_dates = (date(2027, 11, 1), date(2027, 11, 30))
    assert not same_event(  # two named meetings are two events, even on the same dates
        _entry(**CONFERENCE, meetings=AASLD, window=same_dates),
        _entry(**CONFERENCE, meetings=EASL, window=same_dates),
    )


@pytest.mark.parametrize(
    ("quote", "names"),
    [
        ("Data will be presented at the AASLD Liver Meeting in November 2027.", {"aasld liver meeting"}),
        ("to be presented at The Liver Meeting® 2027 in Boston", {"liver meeting"}),
        (
            "at the American Society of Clinical Oncology (ASCO) 2027 Annual Meeting.",
            {"american society of clinical oncology meeting"},
        ),
        ("in an oral session at the 66th ASH Annual Meeting and Exposition", {"ash meeting and exposition"}),
        (
            "at the European Society for Medical Oncology Congress 2027",
            {"european society for medical oncology congress"},
        ),
        ("at the EASL Congress and at the AASLD Liver Meeting", {"easl congress", "aasld liver meeting"}),
        ("at ESMO 2027", set[str]()),  # no meeting word: not read as a name
        ("at the Annual Meeting of stockholders", set[str]()),  # nothing but meeting words
        ("at a medical meeting in the second half of 2027", set[str]()),
        # A session is part of a meeting, never its name (Opus review of 66ecb0f, nit).
        ("presented during the Late-Breaking Session at ESMO 2027", set[str]()),
        ("presented during the Late-Breaking Session at ASCO 2027", set[str]()),
        ("in an Oral Session at the EASL Congress", {"easl congress"}),
    ],
)
def test_meeting_names_are_read_from_the_quote(quote: str, names: set[str]) -> None:
    assert meeting_names(quote) == names


# --- Which entry is kept, and when nothing happens ---


def test_the_higher_ranked_newer_source_is_kept() -> None:
    old, new = _entry(rank=Rank(SEC, OLDER)), _entry(rank=Rank(SEC, NEWER))
    assert merge_plan([old, new]) == [(new, old)]
    filing, wire = _entry(rank=Rank(SEC, OLDER)), _entry(rank=Rank(WIRE, NEWER))
    assert merge_plan([wire, filing]) == [(filing, wire)]  # a higher tier wins over a newer date


def test_a_tie_in_rank_merges_nothing() -> None:
    assert merge_plan([_entry(), _entry()]) == []


def test_a_group_that_is_not_one_event_throughout_merges_nothing() -> None:
    # Both trial entries fit the one without a trial, but not each other: plain code cannot choose.
    a = _entry(trials=frozenset({TRIAL}), rank=Rank(SEC, NEWER))
    b = _entry(trials=frozenset({OTHER_TRIAL}))
    c = _entry(trials=frozenset(), rank=Rank(WIRE, NEWER))
    assert merge_plan([a, b, c]) == []


def test_three_entries_of_one_event_keep_one() -> None:
    a, b, c = _entry(rank=Rank(WIRE, NEWER)), _entry(rank=Rank(SEC, NEWER)), _entry(trials=frozenset())
    assert set(merge_plan([a, b, c])) == {(b, a), (b, c)}


_DRUGS = st.sampled_from(["pegozafermin", "pegozafermin (BIO89-100)", "efruxifermin", "X with Y", "X", None])
_TRIALS = st.sampled_from([frozenset(), frozenset({TRIAL}), frozenset({OTHER_TRIAL})])
_ENTRIES = st.lists(
    st.builds(
        _entry,
        company_id=st.sampled_from([COMPANY, OTHER_COMPANY]),
        catalyst_type=st.sampled_from(["topline_readout", "conference_presentation"]),
        drug=_DRUGS,
        trials=_TRIALS,
        indication=st.sampled_from(["", "mash", "nsclc"]),
        window=st.sampled_from(
            [
                (date(2027, 1, 1), date(2027, 6, 30)),
                (date(2027, 4, 1), date(2027, 6, 30)),
                (date(2027, 7, 1), date(2027, 12, 31)),
            ]
        ),
        meetings=st.sampled_from([frozenset[str](), AASLD, EASL]),
        title=st.sampled_from(
            [TITLE, "Phase 2 topline data for pegozafermin", "Phase 3 interim data", "BLA to FDA"]
        ),
        rank=st.builds(
            Rank,
            st.sampled_from([SEC, WIRE]),
            st.sampled_from([OLDER, NEWER, NEWER + timedelta(days=3)]),
        ),
    ),
    max_size=7,
)


@settings(max_examples=300, deadline=None)
@given(entries=_ENTRIES)
def test_every_merge_is_sure_and_a_second_pass_merges_nothing(entries: list[Entry]) -> None:
    plan = merge_plan(entries)
    kept = {k.id for k, _ in plan}
    dropped = [d.id for _, d in plan]
    assert len(dropped) == len(set(dropped))
    assert not kept & set(dropped)
    for keep, drop in plan:
        assert same_event(keep, drop)
        assert same_event(drop, keep)
        assert keep.window == next(e.window for e in entries if e.id == keep.id)  # never changed
        tier = list(SourceTier).index
        assert (tier(keep.rank.tier), -keep.rank.published_at.timestamp()) < (
            tier(drop.rank.tier),
            -drop.rank.published_at.timestamp(),
        )
        # Every entry it drops is the same event as every other entry of that group.
        group = [e for e in entries if e.id == keep.id or e.id in {d.id for k, d in plan if k.id == keep.id}]
        assert all(same_event(x, y) for x in group for y in group if x.id != y.id)
    left = [e for e in entries if e.id not in set(dropped)]
    assert merge_plan(left) == []  # re-running changes nothing
    assert set(merge_plan(entries[::-1])) == set(plan)  # the order read never changes the outcome


# --- On the database: the nightly write step ---


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime = NOW) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


def _second_name(world: World) -> UUID:
    """Add a second drug record for the same drug, as a stray name in the reference tables makes one."""
    drug = world.add(Drug(company_id=world.company, preferred_name="BIO89-100"))
    world.add(DrugName(drug_id=drug, name="BIO89-100", kind=DrugNameKind.CODE))
    return drug


def _readout(world: World, date_text: str, **changes: Any) -> UUID:
    drug = changes.get("drug", "pegozafermin")
    trial = "the Phase 3 RAINIER trial of " if changes.get("trial") else ""
    sentence = f"Topline data from {trial}{drug} are expected in the {date_text}."
    options: dict[str, Any] = {"trial": False, **changes}
    return world.claim(date_text, sentence=sentence, **options)


def _two_entries(world: World, **second: Any) -> tuple[UUID, UUID, dict[str, Any]]:
    """Publish one readout as pegozafermin, then as "pegozafermin (BIO89-100)".

    Return the older entry, the newer one, and the summary of the run that wrote the newer one: the
    rule runs last in that same run.
    """
    _readout(world, "fourth quarter of 2026", published=OLDER)
    assert _publish(world)["catalysts_created"] == 1
    fields = {"drug": "pegozafermin (BIO89-100)", "drug_id": _second_name(world), "published": NEWER}
    _readout(world, second.pop("date_text", "December 2026"), **(fields | second))
    summary = _publish(world)
    assert summary["catalysts_created"] == 1, summary["top_reasons"]
    older, newer = sorted(world.rows(Catalyst), key=lambda row: row.title != "Topline data: pegozafermin")
    return older.id, newer.id, summary


def _statuses(world: World) -> dict[UUID, str]:
    return {row.id: row.status for row in world.rows(Catalyst)}


def _issue_ids(world: World) -> set[UUID]:
    with world.factory() as session:
        issue = build_digest(published(session), date(2026, 10, 19), access=Access.PAID)
    return {item.catalyst.id for item in issue.moved} | {c.id for c in (*issue.new, *issue.upcoming)}


def test_a_true_duplicate_is_merged_into_the_newer_entry_with_its_history(world: World) -> None:
    older_id, newer_id, summary = _two_entries(world)
    assert summary["duplicates_merged"] == 1
    assert _statuses(world) == {older_id: "merged", newer_id: "upcoming"}
    # The kept entry is as its own claim wrote it: its window and text never change.
    (kept,) = [r for r in world.rows(Catalyst) if r.id == newer_id]
    assert (kept.window_start, kept.window_end, kept.window_raw_text) == (
        date(2026, 12, 1), date(2026, 12, 31), "December 2026"
    )  # fmt: skip
    assert kept.title == "Topline data: BIO89-100"
    kept_changes = {c.claim_id for c in world.rows(CatalystChange) if c.catalyst_id == newer_id}
    assert len(kept_changes) == 1  # only the claim that created it
    # The history row: the status change, citing a plain-code claim that quotes the kept entry's source.
    statuses = [c for c in world.rows(CatalystChange) if c.catalyst_id == older_id and c.field == "status"]
    (change,) = [c for c in statuses if c.old_value is not None]
    assert (change.old_value, change.new_value, change.is_correction) == ("upcoming", "merged", True)
    (claim,) = [c for c in world.rows(Claim) if c.id == change.claim_id]
    assert (claim.agent, claim.value["kept"]) == (DEDUPLICATOR, str(newer_id))
    (window_source,) = [s for s in world.shown(newer_id).sources if "window_start" in s.fields]
    assert claim.quote == window_source.quote
    # The dropped entry's claims are now extra sources of the kept one.
    (link,) = world.rows(CatalystMergedSource)
    assert (link.catalyst_id, link.merged_id) == (newer_id, older_id)
    shown = world.shown(newer_id)
    extra = [s for s in shown.sources if SAME_EVENT in s.fields]
    assert [s.claim_id for s in extra] == [link.claim_id]
    assert "fourth quarter of 2026" in extra[0].quote
    # The API, the export and the digest show one entry.
    with world.factory() as session:
        assert read_catalyst(session, older_id, Access.PAID) is None
        assert [d.id for d in published(session)] == [newer_id]
        page = upcoming(session, Query(), Access.PAID, NOW.date())
        assert [c.id for c in page.catalysts] == [newer_id]
    with world.factory() as session:
        export = build_export(session, NOW, include_held=False)
    assert [c["id"] for c in export["catalysts"]] == [str(newer_id)]
    assert _issue_ids(world) == {newer_id}


def test_re_running_changes_nothing(world: World) -> None:
    assert _two_entries(world)[2]["duplicates_merged"] == 1
    counts = (world.counts(), len(world.rows(Claim)), len(world.rows(CatalystMergedSource)))
    for day in (1, 2):
        assert _publish(world, NOW + timedelta(days=day))["duplicates_merged"] == 0
    assert (world.counts(), len(world.rows(Claim)), len(world.rows(CatalystMergedSource))) == counts


def test_a_later_release_that_fits_only_the_merged_entry_waits_for_a_person(world: World) -> None:
    _two_entries(world)
    statuses = _statuses(world)
    assert sorted(statuses.values()) == ["merged", "upcoming"]
    claim = _readout(world, "December 2026", published=NEWER + timedelta(days=1))  # the merged entry's drug
    summary = _publish(world, NOW + timedelta(days=2))
    assert summary["catalysts_created"] == 0  # the duplicate does not come back
    assert summary["top_reasons"] == {
        MERGED_EVENT: 1
    }  # never sent to the kept entry (Opus review, finding 2)
    assert _check(world, claim) == ("fail", MERGED_EVENT)
    assert _statuses(world) == statuses


def _same_name_record(world: World) -> UUID:
    """Add a second drug record that goes by the same name, as a stray copy in the reference tables does."""
    drug = world.add(Drug(company_id=world.company, preferred_name="pegozafermin"))
    world.add(DrugName(drug_id=drug, name="pegozafermin", kind=DrugNameKind.INN))
    return drug


def test_a_claim_about_another_trial_never_moves_the_kept_entry(world: World) -> None:
    """Opus review of 66ecb0f, finding 2: B (no trial) merged into A (RAINIER); a claim names SYMMETRY."""
    stray = _same_name_record(world)
    _readout(world, "fourth quarter of 2026", published=OLDER, drug_id=stray)  # B: no trial
    _publish(world)
    _readout(world, "December 2026", published=NEWER, trial=True)  # A: RAINIER, the same title
    assert _publish(world)["duplicates_merged"] == 1
    (kept,) = [r for r in world.rows(Catalyst) if r.status == "upcoming"]
    sentence = "Topline data from the Phase 3 SYMMETRY trial of pegozafermin are expected in the March 2027."
    claim = world.claim(
        "March 2027",
        sentence=sentence,
        published=NEWER + timedelta(days=1),
        drug_id=stray,
        trial=world.other_trial,
    )
    summary = _publish(world, NOW + timedelta(days=1))
    assert (summary["catalysts_created"], summary["catalysts_updated"]) == (0, 0)
    assert _check(world, claim) == ("fail", MERGED_EVENT)
    (after,) = [r for r in world.rows(Catalyst) if r.id == kept.id]
    assert (after.window_start, after.window_end) == (kept.window_start, kept.window_end)


def _check(world: World, claim: UUID) -> tuple[str, str | None]:
    (row,) = [r for r in world.rows(ClaimCheck) if r.claim_id == claim and r.check_name == "safe_to_write"]
    return row.outcome, row.reason


def _window_claim(world: World, catalyst_id: UUID) -> UUID:
    (claim_id,) = {c.claim_id for c in world.rows(CatalystChange) if c.catalyst_id == catalyst_id}
    return claim_id


def test_a_withdrawn_kept_entry_takes_its_merged_entry_with_it(world: World) -> None:
    """Opus review of 66ecb0f, finding 3: withdrawing A must not let B's event back in (ADR 0070 cutoff)."""
    _, kept, _ = _two_entries(world)
    entry = Withdrawal(
        claim=str(_window_claim(world, kept)),
        reason="superseded",
        evidence="own source",
        added=date(2026, 11, 25),
    )
    withdrawn_at = NOW + timedelta(days=1)
    summary = publish_claims(
        world.factory, world.store, lambda: withdrawn_at, verifier_model=VERIFIER_MODEL, withdrawals=[entry]
    )
    assert summary.catalysts_withdrawn == 1
    older = _readout(world, "December 2026", published=NEWER + timedelta(hours=2))  # before the withdrawal
    summary = publish_claims(
        world.factory,
        world.store,
        lambda: withdrawn_at + timedelta(days=1),
        verifier_model=VERIFIER_MODEL,
        withdrawals=[entry],
    )
    assert summary.catalysts_created == 0
    assert _check(world, older) == ("fail", WITHDRAWN_EVENT)
    _readout(world, "January 2027", published=withdrawn_at + timedelta(hours=1))  # after it: a new event
    summary = publish_claims(
        world.factory,
        world.store,
        lambda: withdrawn_at + timedelta(days=2),
        verifier_model=VERIFIER_MODEL,
        withdrawals=[entry],
    )
    assert summary.catalysts_created == 1


def test_a_closed_kept_entry_takes_its_merged_entry_with_it(world: World) -> None:
    """Opus review of 66ecb0f, finding 3: a closing (ADR 0080) must not let B's event back in."""
    _, kept, _ = _two_entries(world)
    source = _window_claim(world, kept)
    (claim,) = [c for c in world.rows(Claim) if c.id == source]
    closing = Claim(
        source_document_id=claim.source_document_id,
        subject_type=ClaimSubject.CATALYST,
        subject_id=kept,
        field=f"deal_closed:{kept}",
        value={"status": "resolved", "names": [], "deal_names": [], "own_names": []},
        quote=claim.quote,
        quote_start=claim.quote_start,
        quote_end=claim.quote_end,
        agent=DEAL_CLOSER,
        prompt_id="closed_deals",
        prompt_version="1.1.0",
        provider="plain code",
        model="plain code",
        extracted_at=NOW,
    )
    world.add(closing)
    world.add(
        CatalystChange(
            catalyst_id=kept, field=CatalystField.STATUS, old_value="upcoming", new_value="resolved",
            claim_id=closing.id, learned_at=NOW + timedelta(hours=1),
        )
    )  # fmt: skip
    with world.factory.begin() as session:
        session.execute(
            update(TABLES[Catalyst]).where(TABLES[Catalyst].c.id == kept).values(status="resolved")
        )
    older = _readout(
        world, "December 2026", published=OLDER + timedelta(days=1)
    )  # before the completion document
    summary = _publish(world, NOW + timedelta(days=1))
    assert summary["catalysts_created"] == 0
    assert _check(world, older) == ("fail", CLOSED_EVENT)


@pytest.mark.parametrize(
    "second",
    [
        {"indication": "NSCLC"},  # different indications
        {"date_text": "first half of 2027"},  # windows that do not overlap
        {"trial": True},  # one side names a trial, the other none, and the titles differ: unsure
    ],
)
def test_near_misses_on_the_database(world: World, second: dict[str, Any]) -> None:
    summary = _two_entries(world, **second)[2]
    assert summary["duplicates_merged"] == 0
    assert _publish(world, NOW + timedelta(days=1))["duplicates_merged"] == 0
    assert sorted(_statuses(world).values()) == ["upcoming", "upcoming"]


def _talk(world: World, date_text: str, meeting: str, **changes: Any) -> UUID:
    drug = changes.get("drug", "pegozafermin")
    sentence = f"Data for {drug} will be presented at the {meeting} in the {date_text}."
    return world.claim(
        date_text, sentence=sentence, catalyst_type="conference_presentation", trial=False, **changes
    )


@pytest.mark.parametrize(("meeting", "merges"), [("AASLD Liver Meeting", True), ("EASL Congress", False)])
def test_a_conference_merges_only_for_the_same_meeting(world: World, meeting: str, merges: bool) -> None:
    _talk(world, "fourth quarter of 2026", "AASLD Liver Meeting", published=OLDER)
    _publish(world)
    other = {"drug": "pegozafermin (BIO89-100)", "drug_id": _second_name(world), "published": NEWER}
    _talk(world, "second half of 2026", meeting, **other)  # overlapping, not the same dates
    summary = _publish(world)
    assert (summary["catalysts_created"], summary["duplicates_merged"]) == (1, int(merges))
