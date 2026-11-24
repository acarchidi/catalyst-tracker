"""The pre-publication review's mechanical checks (owner order 2026-11-24; ADR 0055, decision 7).

Each check is judged against docs/issue-review.md, not against the implementation.
"""

from __future__ import annotations

from datetime import date

from catalyst.api.access import Access
from catalyst.outreach.digest import build_digest, render_html, render_page
from catalyst.outreach.review import Finding, review_digest, review_note, review_page

from .test_digest import ISSUE, _detail  # pyright: ignore[reportPrivateUsage]

GOOD_NOTE = (
    "In this week's Catalyst Tracker newsletter: two drugs present updated blood cancer data a day apart.\n\n"
    "Cullinan's CLN-049 in AML on Dec 13.\n\n"
    "Immunome's IM-1021 in B-cell lymphoma on Dec 14.\n\n"
    "Plus 5 programs added to the tracker from last week's filings.\n\n"
    "[paste free post link]"
)


def _checks(findings: list[Finding]) -> set[str]:
    return {f.check for f in findings}


def test_a_clean_issue_passes() -> None:
    clean = _detail(
        start=date(2026, 10, 20), raw="October 20, 2026", quote="topline data on October 20, 2026"
    )
    digest = build_digest([clean], ISSUE)
    assert review_digest(digest) == []
    assert review_page(render_page(digest)) == []


def test_the_window_words_must_be_in_the_quote() -> None:
    record = _detail(start=date(2026, 10, 1), end=date(2026, 12, 31), raw="Q4 2026", quote="data in 2027")
    assert "window_not_in_quote" in _checks(review_digest(build_digest([record], ISSUE)))


def test_a_warrant_unit_or_right_ticker_fails() -> None:
    for ticker in ("KPHMW", "LGNZZ"):
        record = _detail()
        record = record.model_copy(update={"company": record.company.model_copy(update={"ticker": ticker})})
        assert "ticker_not_common_stock" in _checks(review_digest(build_digest([record], ISSUE))), ticker


def test_an_sec_conformed_name_fails() -> None:
    # The check reads the name readers see, the display name (ADR 0068); here it is the raw SEC name.
    for name in ("MACROGENICS INC", "Viridian Therapeutics, Inc.\\DE", "INCYTE CORP"):
        record = _detail(name=name)
        shown = record.company.model_copy(update={"display_name": name})
        record = record.model_copy(update={"company": shown})
        assert "raw_company_name" in _checks(review_digest(build_digest([record], ISSUE))), name


def test_the_companys_own_spelling_passes_the_name_check() -> None:
    # ADR 0068: the SEC name stays in `name`; the issue shows the display name.
    record = _detail(name="MACROGENICS INC")
    shown = record.company.model_copy(update={"display_name": "MacroGenics, Inc."})
    record = record.model_copy(update={"company": shown})
    assert "raw_company_name" not in _checks(review_digest(build_digest([record], ISSUE)))


def test_a_due_entry_whose_window_has_ended_fails() -> None:
    # Built by hand: the builder never lists an ended window, so the check guards hand-made issues.
    ended = _detail(
        start=date(2026, 9, 1), end=date(2026, 10, 4), raw="September 2026", quote="in September 2026"
    )
    digest = build_digest([], ISSUE).model_copy(update={"upcoming": [ended]})
    assert "window_ended" in _checks(review_digest(digest))


def test_the_same_event_twice_fails() -> None:
    one = _detail(quote="topline data in 2026", raw="2026", start=date(2026, 1, 1), end=date(2026, 12, 31))
    two = one.model_copy(update={"id": _detail().id})
    assert "duplicate" in _checks(review_digest(build_digest([one, two], ISSUE)))


def test_when_lines_must_read_as_english() -> None:
    page = render_page(build_digest([], ISSUE)).replace(
        "<p>No date in the next 30 days moved this week.</p>",
        "<ul><li>x<p>When: Company expects a presentation in December 8-11, 2026.</p></li></ul>",
    )
    assert "December 8-11" in page
    page = page.replace(
        "<p>Nothing is due by then.</p>",
        "<ul><li>x<p>When: Company expects data in late this year.</p></li></ul>",
    )
    assert "when_grammar" in _checks(review_page(page))
    fine = render_page(build_digest([], ISSUE)).replace(
        "<p>Nothing is due by then.</p>",
        "<ul><li>x<p>When: Company expects potential data in December 2026.</p></li></ul>",
    )
    assert "in December 2026." in fine
    assert "when_grammar" not in _checks(review_page(fine))


def test_the_notice_must_be_present_small_and_last() -> None:
    page = render_page(build_digest([], ISSUE))
    assert review_page(page) == []
    moved = page.replace("</body>", "<p>After the notice.</p></body>")
    assert "notice_not_last" in _checks(review_page(moved))
    assert "notice_missing" in _checks(
        review_page(page.replace("<small>", "<span>").replace("</small>", "</span>"))
    )


def test_advice_words_outside_the_notice_fail() -> None:
    page = render_page(build_digest([], ISSUE)).replace("<h1>", "<p>Time to buy.</p><h1>", 1)
    assert "advice_words" in _checks(review_page(page))


def test_every_link_must_be_a_source_address_with_a_quote() -> None:
    page = render_page(build_digest([_detail()], ISSUE))
    assert review_page(page) == []
    bad = page.replace("https://www.sec.gov/", "https://example.com/", 1)
    assert "bad_link" in _checks(review_page(bad))


def test_a_paid_page_passes_its_own_checks() -> None:
    page = render_page(build_digest([_detail()], ISSUE, access=Access.PAID))
    assert review_page(page) == []
    assert review_page(render_html(build_digest([_detail()], ISSUE))) == []


def test_the_note_follows_the_owners_rules() -> None:
    assert review_note(GOOD_NOTE) == []
    cases = {
        "note_opening": GOOD_NOTE.replace("In this week's", "This week's"),
        "note_link": GOOD_NOTE.replace("[paste free post link]", "link"),
        "note_length": GOOD_NOTE.replace("a day apart", "a day apart " + "x" * 60),
        "note_banned_words": GOOD_NOTE.replace("two drugs", "two pivotal biotech catalysts"),
        "note_punctuation": GOOD_NOTE.replace("a day apart.", "a day apart!"),
        "note_dash": GOOD_NOTE.replace("a day apart", "a day apart — soon"),
        "note_layout": GOOD_NOTE.replace("\n\nCullinan", "\nCullinan"),
        "note_count": GOOD_NOTE.replace(
            "Plus 5 programs added to the tracker from last week's filings.", "More soon."
        ),
        "note_question": GOOD_NOTE.replace("a day apart.", "a day apart?"),
        "note_hashtag": GOOD_NOTE.replace("AML", "#AML"),
    }
    for check, note in cases.items():
        assert check in _checks(review_note(note)), check
