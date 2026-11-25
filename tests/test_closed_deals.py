"""Closed deals (ADR 0080): a stored completion document closes an open deal entry.

The four entries of the first weekly issue that had to be withdrawn by hand were deals that had
already closed: PTC's ST-920 (8-K Item 2.01), Viatris and Pacira (a "completes acquisition"
release), Harrow and TYRVAYA (8-K Item 2.01), and InMed and Mentari (a release). The pages in
`PAGES` are shaped like those documents (not verbatim copies); their dates are moved to fit the
test clock. Plain code finds the completion sentence; it only ever closes, and never publishes.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st
from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api.access import Access
from catalyst.api.read import catalyst as read_catalyst
from catalyst.api.read import published
from catalyst.collectors.documents import FetchedDocument, store_fetched
from catalyst.db.tables import TABLES
from catalyst.outreach.digest import build_digest
from catalyst.publish.closed_deals import (
    CLOSED_EVENT,
    DEAL_CLOSER,
    completion_sentences,
    counterparties,
    deal_names,
    names_in,
    reports_completion_item,
)
from catalyst.publish.live import Key, LiveCatalyst, LiveIndex, Rank
from catalyst.publish.write import publish_claims
from catalyst.schemas import (
    Catalyst,
    CatalystChange,
    Claim,
    ClaimCheck,
    Company,
    CompanyTicker,
    DocumentMatch,
    SourceTier,
)
from catalyst.snapshots import LocalSnapshotStore
from test_publish_writer import NOW, OCT_14, VERIFIER_MODEL, World

# Pages shaped like the four real completion documents, and the negative cases. They are not
# recorded copies (no live site is fetched from a session), so they live here, not in tests/fixtures,
# whose every page is a recorded copy that other tests pin.
PAGES: dict[str, str] = {
    "agreement-to-acquire-ex99-1.htm": (
        "<html><head><title>ex991.htm</title></head><body>\n"
        "<p>Exhibit 99.1</p>\n"
        "<p><b>InMed Pharmaceuticals Enters into Definitive Agreement to Acquire Mentari Bioscience</b></p>\n"
        "<p>InMed Pharmaceuticals Inc. (Nasdaq: INM) today announced that it has entered into a "
        "definitive agreement to acquire Mentari Bioscience, Inc. The acquisition of Mentari is "
        "expected to be completed in the fourth quarter of 2026, subject to customary closing "
        "conditions.</p>\n"
        "<p>InMed expects that the Mentari transaction will close once the closing conditions have been "
        "completed.</p>\n"
        "<p>Upon closing the acquisition of Mentari, InMed will hold all of its shares.</p>\n"
        "</body></html>\n"
    ),
    "harrow-tyrvaya-8k-item-2-01.htm": (
        "<html><head><title>harrow-20261001.htm</title></head><body>\n"
        "<p>FORM 8-K</p>\n"
        "<p>HARROW, INC.</p>\n"
        "<p>Item 2.01. Completion of Acquisition or Disposition of Assets.</p>\n"
        "<p>On October 1, 2026, Harrow, Inc. (&#8220;Harrow&#8221;) closed the acquisition of the U.S. "
        "commercial rights to TYRVAYA&#174; (varenicline solution) Nasal Spray, as contemplated by the "
        "Asset Purchase Agreement previously reported on the Company&#8217;s Current Report on Form "
        "8-K.</p>\n"
        "<p>Item 9.01. Financial Statements and Exhibits.</p>\n"
        "</body></html>\n"
    ),
    "inmed-mentari-ex99-1.htm": (
        "<html><head><title>ex991.htm</title></head><body>\n"
        "<p>Exhibit 99.1</p>\n"
        "<p><b>InMed Pharmaceuticals Completes Acquisition of Mentari Bioscience</b></p>\n"
        "<p>Vancouver, British Columbia &#8211; November 6, 2026 &#8211; InMed Pharmaceuticals Inc. "
        "(Nasdaq: INM) today announced the completion of its acquisition of Mentari Bioscience, "
        "Inc.</p>\n"
        "<p>The acquisition of Mentari was consummated on November 6, 2026.</p>\n"
        "</body></html>\n"
    ),
    "other-counterparty-ex99-1.htm": (
        "<html><head><title>ex991.htm</title></head><body>\n"
        "<p>Exhibit 99.1</p>\n"
        "<p><b>InMed Pharmaceuticals Completes Acquisition of Zentara Labs</b></p>\n"
        "<p>InMed Pharmaceuticals Inc. (Nasdaq: INM) today announced that it has completed its "
        "acquisition of Zentara Labs, LLC.</p>\n"
        "</body></html>\n"
    ),
    "ptc-st920-8k-item-2-01.htm": (
        "<html><head><title>ptc-20260921.htm</title></head><body>\n"
        "<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>\n"
        "<p>Washington, D.C. 20549</p>\n"
        "<p>FORM 8-K</p>\n"
        "<p>CURRENT REPORT Pursuant to Section 13 or 15(d) of the Securities Exchange Act of 1934</p>\n"
        "<p>PTC Therapeutics, Inc.</p>\n"
        "<p>(Exact name of registrant as specified in its charter)</p>\n"
        "<p>Item 2.01 Completion of Acquisition or Disposition of Assets.</p>\n"
        "<p>On September 21, 2026, PTC Therapeutics, Inc. (the &#8220;Company&#8221;) completed its "
        "previously announced acquisition of the global rights to ST-920 (isaralgagene civaparvovec) "
        "pursuant to the Asset Purchase Agreement dated July 30, 2026.</p>\n"
        "<p>The foregoing description of the Asset Purchase Agreement does not purport to be complete "
        "and is qualified in its entirety by reference to the full text of the agreement.</p>\n"
        "<p>Item 9.01 Financial Statements and Exhibits.</p>\n"
        "<p>SIGNATURES</p>\n"
        "</body></html>\n"
    ),
    "viatris-pacira-ex99-1.htm": (
        "<html><head><title>ex99-1.htm</title></head><body>\n"
        "<p>Exhibit 99.1</p>\n"
        "<p><b>Viatris Completes Acquisition of Pacira BioSciences</b></p>\n"
        "<p>PITTSBURGH and TAMPA, Fla., Nov. 17, 2026 &#8211; Viatris Inc. (NASDAQ: VTRS) today "
        "announced that it has completed its previously announced acquisition of Pacira BioSciences, "
        "Inc. (NASDAQ: PCRX). Pacira&#8217;s common stock will no longer trade on the Nasdaq Global "
        "Select Market.</p>\n"
        "<p>About Viatris</p>\n"
        "<p>Viatris Inc. is a global healthcare company.</p>\n"
        "</body></html>\n"
    ),
}
Q4 = "fourth quarter of 2026"


@pytest.fixture
def world(session_factory: sessionmaker[Session], tmp_path: Path) -> World:
    return World(session_factory, LocalSnapshotStore(tmp_path))


def _publish(world: World, at: datetime) -> dict[str, Any]:
    return publish_claims(world.factory, world.store, lambda: at, verifier_model=VERIFIER_MODEL).summary()


def _company(world: World, cik: int, name: str, ticker: str) -> UUID:
    """Make `name` the company the world's next claims are about."""
    world.company = world.add(Company(cik=cik, name=name, sic_code="2834"))
    world.add(
        CompanyTicker(company_id=world.company, ticker=ticker, exchange="Nasdaq", valid_from=date(2020, 1, 1))
    )
    return world.company


def _deal(world: World, sentence: str, title: str, *, published: datetime = OCT_14) -> UUID:
    claim = world.claim(
        Q4,
        published=published,
        sentence=sentence,
        catalyst_type="merger_acquisition",
        drug=None,
        trial=False,
        indication=None,
    )
    if title:  # the extractor's own title, kept in the claim, which is never published
        _retitle(world, claim, title)
    return claim


def _retitle(world: World, claim: UUID, title: str) -> None:
    table = TABLES[Claim]
    with world.factory.begin() as session:
        value = session.execute(select(table.c.value).where(table.c.id == claim)).scalar_one()
        session.execute(update(table).where(table.c.id == claim).values(value=value | {"title": title}))


def _document(
    world: World, fixture: str, published: datetime, *, kind: str = "8-K EX-99.1", company: UUID | None = None
) -> UUID:
    content = PAGES.get(fixture, fixture).encode()
    key = f"sec:{abs(hash((fixture, published, kind)))}"
    document, _ = store_fetched(
        FetchedDocument(
            source_key=key,
            url=f"https://www.sec.gov/Archives/edgar/data/9/{abs(hash(key))}.htm",
            publisher="SEC EDGAR",
            source_tier=SourceTier.REGULATOR_OR_SEC,
            document_type=kind,
            published_at=published,
            content=content,
            content_type="text/html",
        ),
        world.store,
        world.factory,
        (company or world.company, DocumentMatch.SEC_CIK),
        lambda: published,
    )
    return document.id


def _open_deal(world: World, sentence: str, title: str = "") -> UUID:
    _deal(world, sentence, title)
    assert _publish(world, NOW)["catalysts_created"] == 1
    (row,) = [r for r in world.rows(Catalyst) if r.company_id == world.company]
    assert row.status == "upcoming"
    return row.id


def _status(world: World, catalyst_id: UUID) -> str:
    (row,) = [r for r in world.rows(Catalyst) if r.id == catalyst_id]
    return row.status


DAY = timedelta(days=1)
LATER = NOW + 40 * DAY  # 27 November 2026


# --- The completion sentence, read by plain code ---


@pytest.mark.parametrize(
    "sentence",
    [
        "Viatris Completes Acquisition of Pacira BioSciences",
        "Viatris Inc. today announced that it has completed its previously announced acquisition of Pacira.",
        "On October 1, 2026, Harrow, Inc. closed the acquisition of the U.S. rights to TYRVAYA.",
        "InMed today announced the completion of its acquisition of Mentari Bioscience, Inc.",
        "The acquisition of Mentari was consummated on November 6, 2026.",
        "On November 17, 2026, the Merger became effective.",
        "The merger has been completed.",
    ],
)
def test_a_sentence_that_says_the_deal_happened_counts(sentence: str) -> None:
    assert [c.sentence for c in completion_sentences(sentence)] == [sentence]


@pytest.mark.parametrize(
    "sentence",
    [
        "InMed has entered into a definitive agreement to acquire Mentari Bioscience, Inc.",
        "The acquisition of Mentari is expected to be completed in the fourth quarter of 2026.",
        "InMed expects that the transaction will close once the closing conditions have been completed.",
        "Upon closing the acquisition of Mentari, InMed will hold all of its shares.",
        "The merger has not been completed.",
        "Item 2.01 Completion of Acquisition or Disposition of Assets.",
        "If the merger is not completed, InMed may owe a termination fee.",
        "InMed anticipates it will have completed the acquisition of Mentari by year-end.",
    ],
)
def test_a_plan_a_condition_or_a_heading_does_not_count(sentence: str) -> None:
    assert completion_sentences(sentence) == []


@given(
    st.sampled_from(
        ["is expected to be", "will be", "may be", "should be", "is anticipated to be", "would be"]
    ),
    st.sampled_from(["completed", "closed", "consummated"]),
    st.sampled_from(["acquisition", "merger", "transaction", "business combination"]),
)
def test_a_deal_that_is_still_to_happen_never_counts(modal: str, verb: str, deal: str) -> None:
    assert completion_sentences(f"The {deal} of Mentari {modal} {verb} in December 2026.") == []


@given(
    st.sampled_from(["completed", "closed", "consummated"]),
    st.sampled_from(["acquisition", "merger", "transaction"]),
)
def test_a_deal_that_happened_counts_in_either_voice(verb: str, deal: str) -> None:
    assert len(completion_sentences(f"InMed has {verb} the {deal} of Mentari.")) == 1
    assert len(completion_sentences(f"The {deal} of Mentari was {verb} on November 6, 2026.")) == 1


def test_sentences_are_cut_at_their_own_ends_not_after_inc() -> None:
    text = "Viatris Inc. (NASDAQ: VTRS) has completed its acquisition of Pacira. Pacira will delist."
    (found,) = completion_sentences(text)
    assert found.sentence == "Viatris Inc. (NASDAQ: VTRS) has completed its acquisition of Pacira."
    assert text[found.start : found.end] == found.sentence


def test_names_are_the_distinctive_words_not_the_deal_words() -> None:
    assert names_in("PTC expects the Acquisition of ST-920 to close in the Fourth Quarter of 2026.") == {
        "ptc",
        "st-920",
    }
    assert names_in("Harrow closed the acquisition of TYRVAYA® Nasal Spray.") == {"harrow", "tyrvaya"}
    own = ["PTC Therapeutics, Inc.", "PTCT"]
    assert counterparties(["PTC expects the acquisition of ST-920 to close"], own) == {"st-920"}
    assert (
        counterparties(["The transaction is expected to close in the fourth quarter of 2026."], own) == set()
    )


def test_only_the_names_after_the_deal_word_are_counterparties() -> None:
    """Opus review of PR #163, B2: a regulator, a state or a law is never a counterparty."""
    claim = (
        "The acquisition of Pacira BioSciences is expected to close in the fourth quarter of 2026, subject to"
        " Hart-Scott-Rodino clearance and approval under Delaware law by the Federal Trade Commission."
    )
    assert counterparties([claim], ["Viatris Inc.", "VTRS"]) == {"pacira"}
    by = counterparties(["Closing of the acquisition of Pacira by Viatris"], ["Pacira BioSciences, Inc."])
    assert by == {"viatris"}
    rights = "Harrow expects to close the acquisition of the U.S. rights to TYRVAYA® (varenicline)."
    assert deal_names(rights) == {"tyrvaya"}


def test_a_condition_after_the_completion_words_does_not_count() -> None:
    assert completion_sentences("The merger has been completed, subject to court approval.") == []
    assert completion_sentences("InMed completed the acquisition of Mentari pending the final payment.") == []


def test_a_past_deal_does_not_count() -> None:
    """Opus review of PR #163, B1: "previously completed" and a deal of an earlier year are history."""
    assert completion_sentences("Viatris previously completed its acquisition of Heron.") == []
    assert completion_sentences("In 2021, Pacira completed its acquisition of Flexion.", year=2026) == []
    assert completion_sentences("About Pacira: Pacira completed its acquisition of Flexion.") == []
    this_year = "On November 6, 2026, InMed completed the acquisition of Mentari."
    assert len(completion_sentences(this_year, year=2026)) == 1


def test_a_semicolon_ends_a_sentence() -> None:
    (found,) = completion_sentences("The acquisition of Zentara was completed; Mentari remains pending.")
    assert found.sentence == "The acquisition of Zentara was completed;"


def test_an_8k_reports_item_2_01_or_5_01() -> None:
    assert reports_completion_item("Item 2.01 Completion of Acquisition or Disposition of Assets.") == "2.01"
    assert reports_completion_item("Item 5.01. Changes in Control of Registrant.") == "5.01"
    assert reports_completion_item("Item 8.01 Other Events. The acquisition was completed.") is None


# --- The four real cases ---


def test_ptc_8k_item_2_01_closes_the_st920_entry(world: World) -> None:
    _company(world, 11, "PTC THERAPEUTICS, INC.", "PTCT")
    catalyst = _open_deal(
        world, "PTC expects the acquisition of ST-920 to close in the fourth quarter of 2026."
    )
    document = _document(world, "ptc-st920-8k-item-2-01.htm", NOW + 3 * DAY, kind="8-K")
    summary = _publish(world, LATER)
    assert (summary["deals_closed"], summary["catalysts_created"]) == (1, 0)
    assert _status(world, catalyst) == "resolved"
    (change,) = [c for c in world.rows(CatalystChange) if c.field == "status" and c.old_value is not None]
    assert (change.old_value, change.new_value, change.is_correction) == ("upcoming", "resolved", False)
    (claim,) = [c for c in world.rows(Claim) if c.id == change.claim_id]
    assert (claim.agent, claim.source_document_id, claim.subject_id) == (DEAL_CLOSER, document, catalyst)
    assert claim.quote.startswith("On September 21, 2026, PTC Therapeutics, Inc.")
    assert "completed its previously announced acquisition of the global rights to ST-920" in claim.quote
    assert claim.value["item"] == "2.01"
    with world.factory() as session:  # the read shows the closing with its source and quote
        shown = read_catalyst(session, catalyst, Access.PAID)
    assert shown is not None
    assert shown.catalyst.status == "resolved"
    (source,) = [s for s in shown.catalyst.sources if "status" in s.fields]
    assert (source.quote, source.claim_id) == (claim.quote, claim.id)


def test_viatris_pacira_release_closes_the_entry_on_the_target(world: World) -> None:
    _company(world, 2, "Pacira BioSciences, Inc.", "PCRX")
    catalyst = _open_deal(
        world,
        "The acquisition is expected to close in the fourth quarter of 2026.",
        title="Closing of the acquisition of Pacira by Viatris",
    )
    _document(world, "viatris-pacira-ex99-1.htm", NOW + 30 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 1
    assert _status(world, catalyst) == "resolved"
    (claim,) = [c for c in world.rows(Claim) if c.agent == DEAL_CLOSER]
    assert claim.quote == "Viatris Completes Acquisition of Pacira BioSciences"
    assert claim.value["names"] == ["viatris"]


def test_harrow_8k_item_2_01_closes_the_tyrvaya_entry(world: World) -> None:
    _company(world, 3, "Harrow, Inc.", "HROW")
    catalyst = _open_deal(
        world, "Harrow expects to close the acquisition of TYRVAYA in the fourth quarter of 2026."
    )
    _document(world, "harrow-tyrvaya-8k-item-2-01.htm", NOW + 13 * DAY, kind="8-K")
    assert _publish(world, LATER)["deals_closed"] == 1
    assert _status(world, catalyst) == "resolved"
    (claim,) = [c for c in world.rows(Claim) if c.agent == DEAL_CLOSER]
    assert "closed the acquisition of the U.S. commercial rights to TYRVAYA" in claim.quote


def test_inmed_mentari_release_closes_the_entry(world: World) -> None:
    _company(world, 4, "InMed Pharmaceuticals Inc.", "INM")
    catalyst = _open_deal(
        world, "The acquisition of Mentari Bioscience is expected to close in the fourth quarter of 2026."
    )
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 1
    assert _status(world, catalyst) == "resolved"


def test_a_closed_deal_leaves_every_section_of_the_issue(world: World) -> None:
    _company(world, 4, "InMed Pharmaceuticals Inc.", "INM")
    _open_deal(
        world, "The acquisition of Mentari Bioscience is expected to close in the fourth quarter of 2026."
    )
    monday = date(2026, 11, 30)
    with world.factory() as session:
        before = build_digest(published(session), monday, access=Access.PAID)
    assert len(before.upcoming) == 1
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    _publish(world, LATER)
    with world.factory() as session:
        after = build_digest(published(session), monday, access=Access.PAID)
    assert (after.moved, after.new, after.upcoming) == ([], [], [])


# --- When unsure, nothing happens ---


def _inmed(world: World) -> UUID:
    _company(world, 4, "InMed Pharmaceuticals Inc.", "INM")
    return _open_deal(
        world, "The acquisition of Mentari Bioscience is expected to close in the fourth quarter of 2026."
    )


def test_an_agreement_to_acquire_closes_nothing(world: World) -> None:
    catalyst = _inmed(world)
    _document(world, "agreement-to-acquire-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_another_counterparty_closes_nothing(world: World) -> None:
    catalyst = _inmed(world)
    _document(world, "other-counterparty-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_a_release_dated_before_the_claim_closes_nothing(world: World) -> None:
    catalyst = _inmed(world)
    _document(world, "inmed-mentari-ex99-1.htm", OCT_14 - DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_another_company_s_release_closes_nothing(world: World) -> None:
    catalyst = _inmed(world)
    other = world.add(Company(cik=77, name="Mentari Bioscience, Inc.", sic_code="2834"))
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY, company=other)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_a_claim_that_names_no_counterparty_is_never_closed(world: World) -> None:
    _company(world, 4, "InMed Pharmaceuticals Inc.", "INM")
    catalyst = _open_deal(world, "The merger is expected to close in the fourth quarter of 2026.")
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_an_8k_without_item_2_01_or_5_01_closes_nothing(world: World) -> None:
    catalyst = _inmed(world)
    text = (
        "<html><body><p>FORM 8-K</p><p>Item 8.01 Other Events.</p>"
        "<p>InMed completed the acquisition of Mentari Bioscience in 2019.</p></body></html>"
    )
    _document(world, text, NOW + 19 * DAY, kind="8-K")
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_a_hidden_completion_sentence_closes_nothing(world: World) -> None:
    catalyst = _inmed(world)
    text = (
        "<html><body><p>Exhibit 99.1</p><p style='display:none'>InMed has completed the acquisition of"
        " Mentari Bioscience.</p><p>InMed reports quarterly results.</p></body></html>"
    )
    _document(world, text, NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_an_entry_of_another_type_is_never_closed(world: World) -> None:
    _company(world, 4, "InMed Pharmaceuticals Inc.", "INM")
    world.claim(
        Q4,
        sentence=(
            "Topline data from the Phase 3 RAINIER trial of pegozafermin with Mentari are expected in the"
            " fourth quarter of 2026."
        ),
    )
    assert _publish(world, NOW)["catalysts_created"] == 1
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert [r.status for r in world.rows(Catalyst)] == ["upcoming"]


# --- It only closes, once, and the event does not come back ---


def test_closing_is_safe_to_re_run_and_publishes_nothing_new(world: World) -> None:
    catalyst = _inmed(world)
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 1
    counts = (len(world.rows(Claim)), len(world.rows(CatalystChange)), len(world.rows(Catalyst)))
    summary = _publish(world, LATER + DAY)
    assert (summary["deals_closed"], summary["catalysts_created"], summary["catalysts_updated"]) == (0, 0, 0)
    assert (len(world.rows(Claim)), len(world.rows(CatalystChange)), len(world.rows(Catalyst))) == counts
    assert _status(world, catalyst) == "resolved"


def test_an_older_release_about_the_closed_deal_does_not_bring_it_back(world: World) -> None:
    catalyst = _inmed(world)
    older = _deal(
        world,
        "The acquisition of Mentari Bioscience is expected to close in the fourth quarter of 2026.",
        "",
        published=OCT_14 - DAY,
    )  # same dates: never cited, so it stays marked to publish
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    summary = _publish(world, LATER)
    assert (summary["deals_closed"], summary["catalysts_created"]) == (1, 0)
    assert summary["top_reasons"] == {CLOSED_EVENT: 1}
    held = [r for r in world.rows(ClaimCheck) if r.claim_id == older and r.check_name == "safe_to_write"]
    assert [(r.outcome, r.reason) for r in held] == [("fail", CLOSED_EVENT)]
    assert [r.status for r in world.rows(Catalyst)] == ["resolved"]
    assert _status(world, catalyst) == "resolved"


# --- Opus review of PR #163: whose deal finished, and which claims a closed deal holds ---


def _viatris(world: World) -> UUID:
    _company(world, 21, "Viatris Inc.", "VTRS")
    return _open_deal(
        world, "Viatris expects the acquisition of Pacira BioSciences to close in the fourth quarter of 2026."
    )


def _pacira(world: World) -> UUID:
    _company(world, 22, "Pacira BioSciences, Inc.", "PCRX")
    return _open_deal(
        world,
        "The acquisition is expected to close in the fourth quarter of 2026.",
        title="Closing of the acquisition of Pacira by Viatris",
    )


def _page(*paragraphs: str) -> str:
    return "<html><body>" + "".join(f"<p>{p}</p>" for p in paragraphs) + "</body></html>"


@pytest.mark.parametrize(
    "paragraphs",
    [
        ("Viatris reports results.", "About Pacira", "In 2021, Pacira completed its acquisition of Flexion."),
        ("About Pacira: Pacira completed its acquisition of Flexion Therapeutics.",),
        ("Pacira completed its acquisition of Flexion Therapeutics.",),  # Pacira bought, it was not bought
        ("Viatris previously completed its acquisition of Pacira's rival, Heron.",),
        ("Viatris completed its acquisition of Pacira's rival, Heron.",),
    ],
)
def test_the_acquirer_s_entry_closes_only_when_it_bought_the_counterparty(
    world: World, paragraphs: tuple[str, ...]
) -> None:
    catalyst = _viatris(world)
    _document(world, _page(*paragraphs), NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_the_acquirer_s_entry_closes_on_its_own_completion(world: World) -> None:
    catalyst = _viatris(world)
    done = "Viatris Inc. today announced that it has completed its acquisition of Pacira BioSciences."
    _document(world, _page(done), NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 1
    assert _status(world, catalyst) == "resolved"


@pytest.mark.parametrize(
    "paragraphs",
    [
        ("Pacira reports results.", "About Viatris", "Viatris completed its acquisition of Mylan in 2020."),
        ("Viatris completed its acquisition of Mylan.",),  # Viatris bought someone else
        ("Viatris completed its acquisition of Pacira's rival, Heron.",),
    ],
)
def test_the_target_s_entry_closes_only_when_it_was_bought(world: World, paragraphs: tuple[str, ...]) -> None:
    catalyst = _pacira(world)
    _document(world, _page(*paragraphs), NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def test_a_regulator_named_in_the_claim_never_closes_it(world: World) -> None:
    _company(world, 21, "Viatris Inc.", "VTRS")
    catalyst = _open_deal(
        world,
        "The acquisition of Pacira BioSciences is expected to close in the fourth quarter of 2026, subject to"
        " Hart-Scott-Rodino clearance.",
    )
    other = "Viatris completed its acquisition of Xyzzy Ltd after Hart-Scott-Rodino clearance."
    _document(world, _page(other), NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert _status(world, catalyst) == "upcoming"


def _two_deals(world: World, second: str) -> tuple[UUID, UUID]:
    """InMed with two open deals, told apart by the trial each names; returns (Mentari's, the other)."""
    _company(world, 4, "InMed Pharmaceuticals Inc.", "INM")
    kind: dict[str, Any] = {"catalyst_type": "merger_acquisition", "drug": None, "indication": None}
    first = (
        f"The acquisition of Mentari Bioscience, sponsor of RAINIER trial, is expected to close in the {Q4}."
    )
    world.claim(Q4, sentence=first, **kind)
    other = f"The acquisition of {second}, sponsor of the SYMMETRY trial, is expected to close in the {Q4}."
    world.claim(Q4, sentence=other, trial=world.other_trial, **kind)
    assert _publish(world, NOW)["catalysts_created"] == 2
    by_title = {row.title: row.id for row in world.rows(Catalyst)}
    return by_title["Merger or acquisition: RAINIER trial"], by_title["Merger or acquisition: SYMMETRY trial"]


def test_of_two_open_deals_only_the_one_that_closed_is_closed(world: World) -> None:
    mentari, zentara = _two_deals(world, "Zentara Labs")
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 1
    assert (_status(world, mentari), _status(world, zentara)) == ("resolved", "upcoming")


def test_a_name_two_open_deals_share_closes_neither(world: World) -> None:
    mentari, both = _two_deals(world, "Zentara Labs and Mentari Bioscience")
    _document(world, _page("InMed completed its acquisition of Mentari Bioscience."), NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 0
    assert (_status(world, mentari), _status(world, both)) == ("upcoming", "upcoming")


def test_another_deal_of_the_same_company_is_not_held_by_a_closed_one(world: World) -> None:
    """Opus review of PR #163, B3: only a claim naming the closed deal's counterparty is held."""
    _inmed(world)
    _document(world, "inmed-mentari-ex99-1.htm", NOW + 19 * DAY)
    assert _publish(world, LATER)["deals_closed"] == 1
    _deal(world, "The acquisition of Zentara Labs is expected to close in the fourth quarter of 2026.", "")
    summary = _publish(world, LATER + DAY)
    assert (summary["catalysts_created"], summary["top_reasons"]) == (1, {})


def test_the_hold_follows_the_counterparty_and_the_completion_date() -> None:
    company = UUID(int=1)
    key = Key(company, "merger_acquisition", frozenset(), frozenset(), "")
    row = Catalyst.model_validate(
        {
            "catalyst_type": "merger_acquisition",
            "company_id": company,
            "title": "Merger or acquisition",
            "window_start": date(2026, 10, 1),
            "window_end": date(2026, 12, 31),
            "window_precision": "quarter",
            "window_raw_text": Q4,
            "date_confidence": "company_confirmed",
            "status": "resolved",
        }
    )
    closing = NOW + 19 * DAY
    known = frozenset({"mentari", "inmed", "inm"})  # the deal's counterparties and the company's own names
    rank = Rank(SourceTier.REGULATOR_OR_SEC, closing)
    done = LiveCatalyst(row, key, rank, LATER, known, frozenset({"mentari"}))
    index = LiveIndex(cited={}, catalysts=(), closed=(done,))
    assert index.was_closed(key, closing - DAY, frozenset({"mentari"}))
    assert index.was_closed(key, closing - DAY, frozenset({"inmed"}))  # only the company's own name
    assert index.was_closed(key, closing - DAY, frozenset())  # names no one: held by default
    assert not index.was_closed(key, closing - DAY, frozenset({"zentara"}))  # another deal
    # A party of the closed deal is named: held, even beside a new name (Opus review of PR #163 at f544bf1).
    assert index.was_closed(key, closing - DAY, frozenset({"mentari", "zentara"}))
    assert not index.was_closed(
        key, closing - DAY, frozenset({"inmed", "zentara"})
    )  # own name plus a new one
    assert not index.was_closed(key, closing + DAY, frozenset({"mentari"}))  # newer than the completion


@pytest.mark.parametrize(
    ("side", "completion", "sentence", "title"),
    [
        (  # Opus re-review of PR #163: the bought company's own older release names only itself
            "pacira",
            "viatris-pacira-ex99-1.htm",
            "The acquisition is expected to close in the fourth quarter of 2026.",
            "Closing of Viatris acquisition of Pacira",
        ),
        (
            "pacira",
            "viatris-pacira-ex99-1.htm",
            "The acquisition is expected to close in the fourth quarter of 2026.",
            "",
        ),
        (  # the mirror: the buyer's older release names the bought company
            "viatris",
            "Viatris Inc. today announced that it has completed its acquisition of Pacira BioSciences.",
            "Viatris expects the acquisition to close in the fourth quarter of 2026.",
            "Viatris to acquire Pacira",
        ),
    ],
)
def test_an_older_release_naming_only_this_deal_s_parties_is_held(
    world: World, side: str, completion: str, sentence: str, title: str
) -> None:
    catalyst = _pacira(world) if side == "pacira" else _viatris(world)
    page = completion if completion in PAGES else _page(completion)
    _document(world, page, NOW + 30 * DAY)
    older = _deal(world, sentence, title, published=OCT_14 - DAY)
    summary = _publish(world, LATER)
    assert (summary["deals_closed"], summary["catalysts_created"]) == (1, 0)
    assert summary["top_reasons"] == {CLOSED_EVENT: 1}
    held = [r for r in world.rows(ClaimCheck) if r.claim_id == older and r.check_name == "safe_to_write"]
    assert [(r.outcome, r.reason) for r in held] == [("fail", CLOSED_EVENT)]
    assert [r.status for r in world.rows(Catalyst)] == ["resolved"]
    assert _status(world, catalyst) == "resolved"
