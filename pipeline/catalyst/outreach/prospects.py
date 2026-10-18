"""Load, check, and add to `outreach/prospects.csv`, the list of public professional contacts (ADR 0020).

Plain code only: it reads and writes the file and never contacts anyone. Every row names the
public page where its details were found; an email must appear on that page exactly as written.
"""

from __future__ import annotations

import csv
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

DEFAULT_PATH = Path("outreach/prospects.csv")

Category = Literal[
    "individual_investor_analyst",
    "information_service",
    "institutional_small",
    "investor_community",
    "media",
    "pharma_investor_group",
]
Status = Literal["new", "approved", "contacted", "replied", "declined", "do_not_contact"]
CATEGORIES: tuple[str, ...] = get_args(Category)
STATUSES: tuple[str, ...] = get_args(Status)

COLUMNS: tuple[str, ...] = (
    "category",
    "name",
    "organization",
    "role",
    "email",
    "contact_page_url",
    "source_url",
    "source_checked_on",
    "notes",
    "status",
    "status_changed_on",
)

_EMAIL = re.compile(r"^[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}$")
# Personal mailbox providers: an address there is not a work address (ADR 0020).
_PERSONAL_DOMAINS = frozenset(
    [
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "rocketmail.com",
        "hotmail.com",
        "outlook.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "proton.me",
        "protonmail.com",
        "pm.me",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "zoho.com",
        "yandex.com",
        "hey.com",
        "fastmail.com",
        "comcast.net",
        "verizon.net",
        "att.net",
    ]
)
# The same providers under a country ending, such as yahoo.co.uk or hotmail.fr.
_PERSONAL_NAMES = frozenset({"gmail", "yahoo", "hotmail", "outlook", "aol", "icloud", "protonmail"})


class Prospect(BaseModel):
    """One row of the prospect list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Category
    name: str = Field(min_length=1)
    organization: str = Field(min_length=1)
    role: str = Field(min_length=1)
    email: str | None = None
    contact_page_url: str
    source_url: str
    source_checked_on: date
    notes: str = ""
    status: Status = "new"
    status_changed_on: date | None = None

    @field_validator("email", "status_changed_on", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("name", "organization", "role")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("contact_page_url", "source_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not re.match(r"^https://[^\s/]+\.[^\s/]+(?:/\S*)?$", value):
            raise ValueError("must be an https:// address")
        return value

    @field_validator("email")
    @classmethod
    def _work_email(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if value != value.lower():
            raise ValueError("must be lower-case")
        if not _EMAIL.match(value):
            raise ValueError("is not a valid email address")
        domain = value.rsplit("@", 1)[1]
        if domain in _PERSONAL_DOMAINS or domain.split(".")[0] in _PERSONAL_NAMES:
            raise ValueError("is a personal mailbox, not a work address")
        return value

    @model_validator(mode="after")
    def _dated_status(self) -> Prospect:
        if self.status != "new" and self.status_changed_on is None:
            raise ValueError(f"status_changed_on is required when status is {self.status}")
        return self

    def sort_key(self) -> tuple[str, str, str, str]:
        """File order: category, then organization, then name (case ignored), then page."""
        return (self.category, self.organization.casefold(), self.name.casefold(), self.source_url)

    def person_key(self) -> tuple[str, str]:
        """Return who the row is about, for rows without an email."""
        return (self.organization.casefold(), self.name.casefold())

    def page_key(self) -> tuple[str, str, str]:
        """Return the organization, name, and source page; no two rows share them."""
        return (*self.person_key(), self.source_url)


class ProspectsError(ValueError):
    """The file has rows that break the rules; `problems` lists every one with its line."""

    def __init__(self, problems: list[str]) -> None:
        """Keep every problem, one per bad row or rule."""
        super().__init__("; ".join(problems))
        self.problems = problems


def load_prospects(path: Path) -> list[Prospect]:
    """Read and check every row; raise ProspectsError naming each bad row's line."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != COLUMNS:
            raise ProspectsError([f"line 1: the columns must be exactly {', '.join(COLUMNS)}"])
        rows: list[tuple[int, Prospect]] = []
        problems: list[tuple[int, str]] = []
        for raw in reader:
            try:
                rows.append((reader.line_num, Prospect.model_validate(raw)))
            except ValidationError as error:
                for detail in error.errors():
                    where = ".".join(str(part) for part in detail["loc"]) or "row"
                    problems.append((reader.line_num, f"{where}: {detail['msg']}"))
    problems.extend(_duplicates_and_order(rows))
    if problems:
        raise ProspectsError([f"line {line}: {message}" for line, message in sorted(problems)])
    return [row for _, row in rows]


def _duplicates_and_order(rows: Sequence[tuple[int, Prospect]]) -> list[tuple[int, str]]:
    problems: list[tuple[int, str]] = []
    emails: dict[str, int] = {}
    pages: dict[tuple[str, str, str], int] = {}
    previous: Prospect | None = None
    for line, row in rows:
        if row.email is not None:
            if row.email in emails:
                problems.append((line, f"same email as line {emails[row.email]}"))
            emails.setdefault(row.email, line)
        if row.page_key() in pages:
            problems.append((line, f"same organization, name and source_url as line {pages[row.page_key()]}"))
        pages.setdefault(row.page_key(), line)
        if previous is not None and row.sort_key() < previous.sort_key():
            problems.append((line, "out of order; keep rows sorted by category, organization, name"))
        previous = row
    return problems


def write_prospects(path: Path, rows: Iterable[Prospect]) -> None:
    """Write the rows in file order, in the one format `load_prospects` reads back unchanged."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in sorted(rows, key=Prospect.sort_key):
            values = row.model_dump(mode="json")
            writer.writerow({key: "" if values[key] is None else values[key] for key in COLUMNS})


@dataclass
class MergeResult:
    """The merged list, the rows added, and each row skipped with the reason."""

    rows: list[Prospect]
    added: list[Prospect] = field(default_factory=list[Prospect])
    skipped: list[tuple[Prospect, str]] = field(default_factory=list[tuple[Prospect, str]])


def merge_prospects(existing: Sequence[Prospect], new: Iterable[Prospect]) -> MergeResult:
    """Add rows from a later research run without duplicates; existing rows are never changed.

    A row is skipped when its email is already listed, or, with no email, when the same person at
    the same organization is. A person marked `do_not_contact` is never added back, with or
    without an email. Only rows with status `new` are taken.
    """
    by_email = {row.email.casefold(): row for row in existing if row.email}
    by_person = {row.person_key(): row for row in existing}
    blocked = {row.person_key() for row in existing if row.status == "do_not_contact"}
    pages = {row.page_key() for row in existing}
    result = MergeResult(rows=list(existing))
    for row in new:
        match = by_email.get(row.email.casefold()) if row.email else by_person.get(row.person_key())
        if row.status != "new":
            result.skipped.append((row, "a new row must start as new"))
        elif match is not None:
            result.skipped.append((row, f"already listed as {match.status}"))
        elif row.person_key() in blocked:
            result.skipped.append((row, "the same person is listed as do_not_contact"))
        elif row.page_key() in pages:
            result.skipped.append((row, "already listed with the same organization, name and page"))
        else:
            result.added.append(row)
            result.rows.append(row)
            if row.email:
                by_email[row.email.casefold()] = row
            by_person.setdefault(row.person_key(), row)
            pages.add(row.page_key())
    result.rows.sort(key=Prospect.sort_key)
    return result


def dropped_do_not_contact(previous: Iterable[Prospect], current: Iterable[Prospect]) -> list[Prospect]:
    """Rows marked `do_not_contact` before that are gone now; they must stay so no one re-adds them."""
    kept = {row.page_key() for row in current}
    return [row for row in previous if row.status == "do_not_contact" and row.page_key() not in kept]
