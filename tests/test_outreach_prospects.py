"""The outreach prospect list: every row follows ADR 0020, and later research adds to it safely."""

import csv
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from catalyst.outreach.prospects import (
    CATEGORIES,
    COLUMNS,
    DEFAULT_PATH,
    STATUSES,
    Prospect,
    ProspectsError,
    dropped_do_not_contact,
    load_prospects,
    merge_prospects,
    write_prospects,
)

GOOD: dict[str, str] = {
    "category": "media",
    "name": "Jane Reporter",
    "organization": "Example News",
    "role": "Reporter",
    "email": "jane@examplenews.com",
    "contact_page_url": "https://examplenews.com/contact",
    "source_url": "https://examplenews.com/staff",
    "source_checked_on": "2026-10-17",
    "notes": "Covers biotech",
    "status": "new",
    "status_changed_on": "",
}


def _write(path: Path, rows: list[dict[str, str]]) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(**changes: str) -> dict[str, str]:
    return GOOD | changes


def _prospect(**changes: str) -> Prospect:
    return Prospect.model_validate(_row(**changes))


def test_the_committed_file_loads_with_every_row_new() -> None:
    rows = load_prospects(DEFAULT_PATH)
    assert len(rows) == 209
    assert Counter(row.status for row in rows) == {"new": 209}
    assert sum(row.email is not None for row in rows) == 115
    assert Counter(row.category for row in rows) == {
        "institutional_small": 56,
        "pharma_investor_group": 30,
        "individual_investor_analyst": 26,
        "information_service": 31,
        "media": 49,
        "investor_community": 17,
    }


def test_the_committed_file_is_written_in_its_own_order_and_format(tmp_path: Path) -> None:
    out = tmp_path / "again.csv"
    write_prospects(out, load_prospects(DEFAULT_PATH))
    assert out.read_text(encoding="utf-8") == DEFAULT_PATH.read_text(encoding="utf-8")


def test_a_good_row_loads(tmp_path: Path) -> None:
    (row,) = load_prospects(_write(tmp_path / "p.csv", [GOOD]))
    assert row.email == "jane@examplenews.com"
    assert row.status_changed_on is None


def test_a_missing_email_is_allowed(tmp_path: Path) -> None:
    (row,) = load_prospects(_write(tmp_path / "p.csv", [_row(email="")]))
    assert row.email is None


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"name": ""}, "name"),
        ({"organization": " "}, "organization"),
        ({"role": ""}, "role"),
        ({"category": "hedge_fund"}, "category"),
        ({"source_url": "http://examplenews.com/staff"}, "https"),
        ({"contact_page_url": "examplenews.com/contact"}, "https"),
        ({"source_url": ""}, "https"),
        ({"email": "Jane@examplenews.com"}, "lower-case"),
        ({"email": "jane.examplenews.com"}, "email"),
        ({"email": "jane@examplenews"}, "email"),
        ({"email": "jane@gmail.com"}, "personal"),
        ({"email": "jane@yahoo.co.uk"}, "personal"),
        ({"email": "jane@protonmail.com"}, "personal"),
        ({"email": "jane@icloud.com"}, "personal"),
        ({"source_checked_on": "17 October 2026"}, "source_checked_on"),
        ({"status": "emailed"}, "status"),
        ({"status": "contacted"}, "status_changed_on"),
        ({"status": "do_not_contact", "status_changed_on": ""}, "status_changed_on"),
    ],
)
def test_each_rule_rejects_a_bad_row(tmp_path: Path, changes: dict[str, str], message: str) -> None:
    path = _write(tmp_path / "p.csv", [_row(**changes)])
    with pytest.raises(ProspectsError, match=message) as caught:
        load_prospects(path)
    assert caught.value.problems[0].startswith("line 2:")


def test_a_changed_status_with_its_date_loads(tmp_path: Path) -> None:
    path = _write(tmp_path / "p.csv", [_row(status="declined", status_changed_on="2026-10-20")])
    (row,) = load_prospects(path)
    assert row.status == "declined"


def test_every_status_is_one_of_the_six() -> None:
    assert STATUSES == ("new", "approved", "contacted", "replied", "declined", "do_not_contact")
    assert len(CATEGORIES) == 6


def test_a_duplicate_email_is_rejected(tmp_path: Path) -> None:
    other = _row(name="John Editor", source_url="https://examplenews.com/editors")
    with pytest.raises(ProspectsError, match="same email as line 2"):
        load_prospects(_write(tmp_path / "p.csv", [GOOD, other]))


def test_a_duplicate_organization_name_and_page_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ProspectsError, match="same organization, name and source_url as line 2"):
        load_prospects(_write(tmp_path / "p.csv", [_row(email=""), _row(email="", role="Editor")]))


def test_rows_out_of_order_are_rejected(tmp_path: Path) -> None:
    first = _row(organization="Zeta News", email="a@zeta.com")
    second = _row(organization="Alpha News", email="b@alpha.com")
    with pytest.raises(ProspectsError, match="out of order"):
        load_prospects(_write(tmp_path / "p.csv", [first, second]))


def test_wrong_columns_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "p.csv"
    path.write_text("category,name\nmedia,Jane\n", encoding="utf-8")
    with pytest.raises(ProspectsError, match="columns"):
        load_prospects(path)


def test_every_bad_row_is_reported_not_only_the_first(tmp_path: Path) -> None:
    rows = [_row(category="x"), _row(organization="Other", email="jane@gmail.com")]
    with pytest.raises(ProspectsError) as caught:
        load_prospects(_write(tmp_path / "p.csv", rows))
    assert [problem[:7] for problem in caught.value.problems] == ["line 2:", "line 3:"]


def test_merge_adds_only_new_contacts_and_reports_the_rest() -> None:
    existing = [_prospect()]
    new = [
        _prospect(),  # same email
        _prospect(name="Jane R.", source_url="https://examplenews.com/jane"),  # same email again
        _prospect(organization="Other News", name="Desk", email="", source_url="https://other.com/c"),
        _prospect(organization="Other News", name="Desk", email="", source_url="https://other.com/d"),
    ]
    result = merge_prospects(existing, new)
    assert [row.organization for row in result.added] == ["Other News"]
    assert len(result.skipped) == 3
    assert all(reason for _row_, reason in result.skipped)
    assert len(result.rows) == 2


def test_merge_matches_an_email_whatever_its_case() -> None:
    upper: dict[str, Any] = _row(name="Jane R.", source_url="https://examplenews.com/jane")
    upper["email"] = "JANE@examplenews.com"
    candidate = Prospect.model_construct(**upper)  # a research row not yet lower-cased
    result = merge_prospects([_prospect()], [candidate])
    assert result.added == []


def test_merge_is_idempotent() -> None:
    existing = [_prospect()]
    new = [_prospect(organization="Other News", email="desk@other.com", source_url="https://other.com/c")]
    once = merge_prospects(existing, new)
    twice = merge_prospects(once.rows, new)
    assert twice.rows == once.rows
    assert twice.added == []


def test_merge_never_changes_an_existing_status() -> None:
    existing = [_prospect(status="replied", status_changed_on="2026-10-20")]
    result = merge_prospects(existing, [_prospect()])
    assert result.rows == existing


def test_merge_never_re_adds_a_do_not_contact_contact() -> None:
    blocked = _prospect(status="do_not_contact", status_changed_on="2026-10-20")
    again = _prospect(role="Senior Reporter", source_url="https://examplenews.com/new-staff-page")
    result = merge_prospects([blocked], [again])
    assert result.rows == [blocked]
    assert "do_not_contact" in result.skipped[0][1]


def test_merge_never_re_adds_a_do_not_contact_person_found_later_with_an_email() -> None:
    blocked = _prospect(email="", status="do_not_contact", status_changed_on="2026-10-20")
    found = _prospect(email="jane.r@examplenews.com", source_url="https://examplenews.com/jane")
    result = merge_prospects([blocked], [found])
    assert result.rows == [blocked]
    assert "do_not_contact" in result.skipped[0][1]


def test_merge_takes_new_rows_only_as_new() -> None:
    sneaky = _prospect(
        organization="Other News",
        email="desk@other.com",
        source_url="https://other.com/c",
        status="approved",
        status_changed_on="2026-10-20",
    )
    result = merge_prospects([], [sneaky])
    assert result.added == []
    assert "must start as new" in result.skipped[0][1]


def test_merge_keeps_the_file_in_order(tmp_path: Path) -> None:
    existing = [_prospect(organization="Zeta News", email="z@zeta.com")]
    result = merge_prospects(existing, [_prospect(organization="Alpha News", email="a@alpha.com")])
    path = tmp_path / "p.csv"
    write_prospects(path, result.rows)
    assert [row.organization for row in load_prospects(path)] == ["Alpha News", "Zeta News"]


def test_a_removed_do_not_contact_row_is_found() -> None:
    blocked = _prospect(status="do_not_contact", status_changed_on="2026-10-20")
    assert dropped_do_not_contact([blocked], [blocked]) == []
    assert dropped_do_not_contact([blocked], []) == [blocked]
    assert dropped_do_not_contact([_prospect()], []) == []


def test_merge_skips_an_email_found_later_on_a_page_already_listed() -> None:
    listed = _prospect(email="")
    result = merge_prospects([listed], [_prospect(email="desk@examplenews.com")])
    assert result.rows == [listed]
    assert "same organization, name and page" in result.skipped[0][1]
