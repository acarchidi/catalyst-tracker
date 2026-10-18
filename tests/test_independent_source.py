"""Independent source: a claim needs a second document that states the same event (spec: Publish rule).

The publish rule asks that "either a second source agrees or the source is a regulator or SEC
filing" (ADR 0018, part 3). Written before the check.
"""

from datetime import date
from uuid import UUID, uuid4

from catalyst.schemas import SourceTier
from catalyst.verify.independent import CHECK, Statement, independent_source
from catalyst.verify.rules import Outcome

SPYRE, OTHER_COMPANY = uuid4(), uuid4()
ZENTOCEL = uuid4()
NO_SECOND = "no second document states this event"


def _statement(
    *,
    url: str = "https://www.prnewswire.com/news-releases/spyre-1.html",
    sha256: str = "a" * 64,
    quote: str = "Zentocel topline data are expected in the second half of 2027.",
    tier: SourceTier = SourceTier.WIRE,
    companies: frozenset[UUID] = frozenset({SPYRE}),
    catalyst_type: str | None = "topline_readout",
    drug: str | None = "zentocel",
    drug_ids: frozenset[UUID] = frozenset(),
    window: tuple[date, date] | None = (date(2027, 7, 1), date(2027, 12, 31)),
    clean: bool = True,
) -> Statement:
    return Statement(
        claim_id=uuid4(),
        url=url,
        sha256=sha256,
        quote=quote,
        tier=tier,
        companies=companies,
        catalyst_type=catalyst_type,
        drug=drug,
        drug_ids=drug_ids,
        window=window,
        clean=clean,
    )


def _elsewhere(
    *,
    url: str = "https://www.spyre.com/news/2026-10-01",
    sha256: str = "b" * 64,
    quote: str = "We expect to report topline results for zentocel in 2H27.",
    companies: frozenset[UUID] = frozenset({SPYRE}),
    catalyst_type: str | None = "topline_readout",
    drug: str | None = "zentocel",
    drug_ids: frozenset[UUID] = frozenset(),
    window: tuple[date, date] | None = (date(2027, 7, 1), date(2027, 12, 31)),
    clean: bool = True,
) -> Statement:
    """Return the same event in the company's own words on its own news page: another source."""
    return _statement(
        url=url,
        sha256=sha256,
        quote=quote,
        tier=SourceTier.COMPANY_SITE,
        companies=companies,
        catalyst_type=catalyst_type,
        drug=drug,
        drug_ids=drug_ids,
        window=window,
        clean=clean,
    )


def _outcome(claim: Statement, *others: Statement) -> tuple[Outcome, str | None]:
    result = independent_source(claim, [claim, *others])
    assert result.check == CHECK
    return result.outcome, result.reason


def test_a_regulator_or_sec_filing_needs_no_second_source() -> None:
    assert _outcome(_statement(tier=SourceTier.REGULATOR_OR_SEC)) == (Outcome.PASS, None)


def test_a_release_with_no_second_document_fails() -> None:
    assert _outcome(_statement()) == (Outcome.FAIL, NO_SECOND)


def test_another_document_with_the_same_event_passes() -> None:
    assert _outcome(_statement(), _elsewhere()) == (Outcome.PASS, None)


def test_a_narrower_window_inside_the_claims_agrees() -> None:
    fourth_quarter = _elsewhere(window=(date(2027, 10, 1), date(2027, 12, 31)))
    assert _outcome(_statement(), fourth_quarter)[0] is Outcome.PASS


def test_a_vaguer_window_does_not_vouch_for_a_precise_one() -> None:
    """A window of "2027" elsewhere confirms the year, not the half (Opus review of PR #46)."""
    whole_year = _elsewhere(window=(date(2027, 1, 1), date(2027, 12, 31)))
    assert _outcome(_statement(), whole_year) == (Outcome.FAIL, NO_SECOND)


def test_a_window_reaching_past_the_claims_does_not_agree() -> None:
    later = _elsewhere(window=(date(2027, 10, 1), date(2028, 3, 31)))
    assert _outcome(_statement(), later)[0] is Outcome.FAIL


def test_a_window_that_does_not_overlap_does_not_agree() -> None:
    first_half = _elsewhere(window=(date(2027, 1, 1), date(2027, 6, 30)))
    assert _outcome(_statement(), first_half) == (Outcome.FAIL, NO_SECOND)


def test_a_missing_window_never_agrees() -> None:
    assert _outcome(_statement(), _elsewhere(window=None))[0] is Outcome.FAIL
    assert _outcome(_statement(window=None), _elsewhere(window=None))[0] is Outcome.FAIL


def test_the_same_document_never_counts_twice() -> None:
    """Two claims from one document, or two versions at one address, are one source."""
    same_page = _statement(sha256="c" * 64, quote="Data from zentocel are due in 2H27.")
    assert _outcome(_statement(), same_page)[0] is Outcome.FAIL


def test_one_address_written_differently_is_one_source() -> None:
    site = "https://www.spyre.com/news/2026-10-01"
    for variant in (
        site + "/",
        site + "?utm_source=x",
        site + "#top",
        "https://WWW.Spyre.com/news/2026-10-01",
        "https://spyre.com/news/2026-10-01",
        "http://www.spyre.com/news/2026-10-01",
    ):
        assert (
            _outcome(_elsewhere(url=site), _elsewhere(url=variant, sha256="c" * 64, quote="Other words."))[0]
            is Outcome.FAIL
        ), variant


def test_copies_of_one_release_are_one_source() -> None:
    """The same file, or the same sentence word for word, is one source wherever it is posted."""
    same_file = _elsewhere(sha256="a" * 64)
    assert _outcome(_statement(), same_file)[0] is Outcome.FAIL
    same_sentence = _elsewhere(quote="Zentocel topline data are expected in the second half of 2027.")
    assert _outcome(_statement(), same_sentence)[0] is Outcome.FAIL
    reflowed = _elsewhere(quote="Zentocel  topline data are expected in the Second Half of 2027")
    assert _outcome(_statement(), reflowed)[0] is Outcome.FAIL


def test_a_missing_type_never_agrees() -> None:
    assert _outcome(_statement(catalyst_type=None), _elsewhere(catalyst_type=None))[0] is Outcome.FAIL


def test_another_company_does_not_agree() -> None:
    assert _outcome(_statement(), _elsewhere(companies=frozenset({OTHER_COMPANY})))[0] is Outcome.FAIL


def test_a_document_linked_to_no_company_never_agrees() -> None:
    assert _outcome(_statement(companies=frozenset()), _elsewhere(companies=frozenset()))[0] is Outcome.FAIL


def test_another_type_does_not_agree() -> None:
    assert _outcome(_statement(), _elsewhere(catalyst_type="regulatory_submission"))[0] is Outcome.FAIL


def test_drugs_agree_by_name_or_by_the_reference_tables() -> None:
    assert _outcome(_statement(drug="VX-548"), _elsewhere(drug="vx 548"))[0] is Outcome.PASS
    brand = _statement(drug="BRIUMVI", drug_ids=frozenset({ZENTOCEL}))
    generic = _elsewhere(drug="ublituximab", drug_ids=frozenset({ZENTOCEL}))
    assert _outcome(brand, generic)[0] is Outcome.PASS
    assert _outcome(_statement(), _elsewhere(drug="other-drug"))[0] is Outcome.FAIL


def test_a_missing_drug_agrees_only_for_a_deal() -> None:
    assert _outcome(_statement(drug=None), _elsewhere(drug=None))[0] is Outcome.FAIL
    assert _outcome(_statement(), _elsewhere(drug=None))[0] is Outcome.FAIL
    deal, other_deal = (
        _statement(catalyst_type="merger_acquisition", drug=None),
        _elsewhere(catalyst_type="merger_acquisition", drug=None),
    )
    assert _outcome(deal, other_deal)[0] is Outcome.PASS


def test_a_flagged_claim_elsewhere_does_not_vouch() -> None:
    """A claim the extractor itself flagged cannot vouch for another (as in the second reading)."""
    assert _outcome(_statement(), _elsewhere(clean=False))[0] is Outcome.FAIL


def test_the_order_of_other_documents_does_not_matter() -> None:
    claim = _statement()
    others = [_elsewhere(drug="other-drug"), _elsewhere(), _elsewhere(companies=frozenset())]
    assert independent_source(claim, others) == independent_source(claim, list(reversed(others)))
