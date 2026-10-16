"""Build the coverage list: US-listed therapeutics developers under SEC industry codes 2834, 2836, 8731.

Steps (ADR 0009):

1. EDGAR's company browse lists every registrant filed under each industry code.
2. SEC's ticker file keeps those listed on a US exchange (not over the counter).
3. ClinicalTrials.gov confirms a therapeutics developer: the company must be the industry lead
   sponsor of at least one interventional drug or biological trial, under its own name.
4. `config/coverage_overrides.toml` adds registry names that differ from the SEC name, or
   includes or excludes a company by hand, with a note.

Listed companies without that evidence are written to a review list, not dropped silently.
"""

from __future__ import annotations

import json
import re
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from defusedxml import ElementTree
from pydantic import BaseModel, ConfigDict, Field

from catalyst.collectors.http import PoliteHttpClient
from catalyst.collectors.sec.edgar import EdgarClient
from catalyst.collectors.sources import ctgov

SIC_CODES = (2834, 2836, 8731)
BROWSE_URL = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
EDGAR_COMPANY_URL = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
NOT_LISTED = frozenset({"OTC"})
BROWSE_PAGE_SIZE = 100
MAX_BROWSE_PAGES = 60
TRIAL_EVIDENCE = (
    "AREA[LeadSponsorClass]INDUSTRY AND AREA[StudyType]INTERVENTIONAL"
    " AND (AREA[InterventionType]DRUG OR AREA[InterventionType]BIOLOGICAL)"
)
TRIAL_PAGE_SIZE = 1000
_ATOM = "{http://www.w3.org/2005/Atom}"

# Words that name a legal form, not a company, removed from the end of a name.
_LEGAL_WORDS = (
    "inc|incorporated|corp|corporation|co|company|ltd|limited|plc|llc|lp|sa|ag|nv|se|ab|as|asa"
    "|spa|bv|gmbh|kk|oyj|and"
)
_TRAILING_LEGAL = re.compile(rf"[\W_]*\b(?:{_LEGAL_WORDS})\b[\W_]*$")
_STATE_SUFFIX = re.compile(r"\s*[/\\]\s*[^\W\d_]{2,3}\s*[/\\]?[\W_]*$")  # EDGAR's "/DE/", " / MA", "\\DE\\"
_WORD = re.compile(r"[^\W_]+")
_DOTTED = re.compile(r"\b(?:[^\W\d_]\.){2,}")  # "S.A.", "N.V.", "S.p.A."
_SLASHED = re.compile(r"\b([^\W\d_])/([^\W\d_])\b")  # "A/S"
_SUBSIDIARY_OF = re.compile(r"\bsubsidiary\s+of\s+", re.IGNORECASE)
_A_COMPANY = re.compile(r"(?:^|[,(\-\u2013]\s*)an?\s+(?P<parent>[^,()]+?)\s+company\s*\)?\s*$", re.IGNORECASE)
_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")


def normalize_name(name: str) -> str:
    """Return the name in a form two spellings of one company share: lower case, words only."""
    text = name.casefold().replace("&", " and ")
    text = _DOTTED.sub(lambda match: match.group(0).replace(".", ""), text)
    text = _SLASHED.sub(r"\1\2", text)
    while True:
        shorter = _STATE_SUFFIX.sub("", text)
        shorter = _TRAILING_LEGAL.sub("", shorter)
        if shorter == text:
            break
        text = shorter
    return " ".join(_WORD.findall(text))


def same_company(sec_name: str, registry_name: str) -> bool:
    """Return True when both names normalize to the same, non-empty name."""
    normalized = normalize_name(sec_name)
    return bool(normalized) and normalized == normalize_name(registry_name)


def stated_parent(sponsor: str) -> str | None:
    """Return the parent a sponsor name states ("…, a GSK Company", "…, a subsidiary of X"), if any."""
    parts = _SUBSIDIARY_OF.split(sponsor)
    if len(parts) > 1:
        return _PARENTHETICAL.sub("", parts[-1])
    match = _A_COMPANY.search(sponsor)
    return match.group("parent") if match else None


def sponsor_of(sec_name: str, sponsor: str) -> bool:
    """Return True when the sponsor is the company, or says in its name that the company is its parent."""
    parent = stated_parent(sponsor)
    return same_company(sec_name, sponsor) or (parent is not None and same_company(sec_name, parent))


@dataclass(frozen=True)
class Listing:
    """A company listed on a US exchange, as SEC's ticker file names it."""

    cik: int
    name: str
    tickers: tuple[str, ...]
    exchange: str

    @property
    def ticker(self) -> str:
        """The main ticker: the first without a class or rights suffix, else the first."""
        return next((t for t in self.tickers if "-" not in t), self.tickers[0])


def browse_page_ciks(xml: bytes) -> tuple[list[int], bool]:
    """Return the SEC numbers on one EDGAR company-browse Atom page, and whether a next page exists."""
    root = ElementTree.fromstring(xml)
    found = [*root.iter(f"{_ATOM}cik"), *root.iter("cik")]  # inside the feed's default namespace
    ciks = [int(cik.text) for cik in found if cik.text and cik.text.strip().isdigit()]
    more = any(link.get("rel") == "next" for link in root.iter(f"{_ATOM}link"))
    return ciks, more


def sic_ciks(sec: EdgarClient, sic: int) -> list[int]:
    """Return every SEC number EDGAR files under `sic`."""
    ciks: list[int] = []
    for page in range(MAX_BROWSE_PAGES):
        params: dict[str, str | int] = {
            "action": "getcompany",
            "SIC": sic,
            "owner": "include",
            "start": page * BROWSE_PAGE_SIZE,
            "count": BROWSE_PAGE_SIZE,
            "output": "atom",
        }
        found, more = browse_page_ciks(sec.get(BROWSE_URL, params=params).content)
        ciks += found
        if not more or not found:
            break
    return ciks


def listed_companies(payload: bytes) -> dict[int, Listing]:
    """Return companies on a US exchange from SEC's company_tickers_exchange.json."""
    data: dict[str, Any] = json.loads(payload)
    position = {name: index for index, name in enumerate(data["fields"])}
    names: dict[int, str] = {}
    tickers: dict[int, list[str]] = {}
    exchanges: dict[int, str] = {}
    for row in data["data"]:
        exchange = row[position["exchange"]]
        if not exchange or exchange in NOT_LISTED:
            continue
        cik = int(row[position["cik"]])
        names.setdefault(cik, row[position["name"]])
        exchanges.setdefault(cik, exchange)
        tickers.setdefault(cik, []).append(row[position["ticker"]])
    return {cik: Listing(cik, names[cik], tuple(tickers[cik]), exchanges[cik]) for cik in names}


def trial_sponsors(web: PoliteHttpClient, company_name: str) -> Counter[str]:
    """Count the company's own industry drug or biological trials, by registered sponsor name."""
    search = normalize_name(company_name)
    if not search:
        return Counter()
    params: dict[str, str | int] = {
        "query.spons": search,
        "filter.advanced": TRIAL_EVIDENCE,
        "fields": "protocolSection.sponsorCollaboratorsModule.leadSponsor",
        "pageSize": TRIAL_PAGE_SIZE,
    }
    studies: list[dict[str, Any]] = web.get(ctgov.API_URL, params=params).json().get("studies", [])
    sponsors = (ctgov.lead_sponsor(study) for study in studies)
    return Counter(name for name in sponsors if sponsor_of(company_name, name))


class Override(BaseModel):
    """A hand-checked correction for one company (config/coverage_overrides.toml)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cik: int = Field(ge=1)
    note: str = Field(min_length=1, description="Why the correction is right, with its source.")
    include: bool | None = None
    ctgov_sponsors: frozenset[str] = frozenset()
    openfda_manufacturer: str | None = None
    openfda_sponsors: frozenset[str] = frozenset()


def load_overrides(path: Path) -> dict[int, Override]:
    """Read the overrides file; a missing file means no overrides."""
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        entries = tomllib.load(handle).get("company", [])
    return {override.cik: override for override in map(Override.model_validate, entries)}


@dataclass
class CoverageEntry:
    """One listed company under a covered industry code, with the evidence found for it."""

    listing: Listing
    sic: int
    sponsors: Counter[str] = field(default_factory=Counter[str])
    override: Override | None = None

    @property
    def included(self) -> bool:
        """Included on trial evidence or by override; an override's `include` always wins."""
        if self.override and self.override.include is not None:
            return self.override.include
        return bool(self.sponsors) or bool(self.override and self.override.ctgov_sponsors)

    @property
    def ctgov_sponsors(self) -> list[str]:
        """Every registered sponsor name for the company: found ones plus hand-added ones."""
        added = self.override.ctgov_sponsors if self.override else frozenset[str]()
        return sorted(set(self.sponsors) | added)


def build_coverage(
    sec: EdgarClient,
    web: PoliteHttpClient,
    overrides: dict[int, Override],
    sic_codes: tuple[int, ...] = SIC_CODES,
) -> list[CoverageEntry]:
    """Return every US-listed company under `sic_codes`, with its trial evidence, by SEC number."""
    listed = listed_companies(sec.get(TICKERS_URL).content)
    entries: dict[int, CoverageEntry] = {}
    for sic in sic_codes:
        for cik in sic_ciks(sec, sic):
            if cik in listed and cik not in entries:
                entries[cik] = CoverageEntry(listed[cik], sic, override=overrides.get(cik))
    for entry in entries.values():
        if not (entry.override and entry.override.include is False):
            entry.sponsors = trial_sponsors(web, entry.listing.name)
    return [entries[cik] for cik in sorted(entries)]


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def coverage_toml(entries: list[CoverageEntry], built_on: date) -> str:
    """Return the included companies in the company-list format the collectors read."""
    included = [e for e in entries if e.included]
    lines = [
        f"# Coverage list built {built_on.isoformat()} by `catalyst coverage-build` (ADR 0009). Do not edit:",
        "# corrections go in config/coverage_overrides.toml. "
        f"{len(included)} of {len(entries)} listed companies included.",
    ]
    for entry in included:
        listing, override = entry.listing, entry.override
        evidence = sum(entry.sponsors.values())
        lines += [
            "",
            "[[company]]",
            f"ticker = {_toml_string(listing.ticker)}",
            f"cik = {listing.cik}",
            f"name = {_toml_string(listing.name)}",
            f"sic = {entry.sic}",
            f"exchange = {_toml_string(listing.exchange)}",
            f"ctgov_sponsors = [{', '.join(map(_toml_string, entry.ctgov_sponsors))}]",
            f"ctgov_trials = {evidence}",
        ]
        if override and override.openfda_manufacturer:
            lines.append(f"openfda_manufacturer = {_toml_string(override.openfda_manufacturer)}")
        if override and override.openfda_sponsors:
            names = ", ".join(map(_toml_string, sorted(override.openfda_sponsors)))
            lines.append(f"openfda_sponsors = [{names}]")
    return "\n".join(lines) + "\n"


def unconfirmed_markdown(entries: list[CoverageEntry], built_on: date) -> str:
    """Return the review list: listed companies left out, with the reason and an EDGAR link."""
    left_out = [e for e in entries if not e.included]
    lines = [
        "# Coverage review list",
        "",
        " ".join(
            [
                f"Built {built_on.isoformat()} by `catalyst coverage-build` (ADR 0009).",
                f"These {len(left_out)} companies are listed on a US exchange under SEC industry codes",
                "2834, 2836, or 8731 but are not in the coverage list. Most have no industry drug or",
                "biological trial on ClinicalTrials.gov under their SEC name: they may not develop",
                "therapeutics, or they may register trials under another name. A correction in",
                "`config/coverage_overrides.toml` adds a company.",
            ]
        ),
        "",
        "| Company | Ticker | Industry code | Reason | SEC filings |",
        "| --- | --- | --- | --- | --- |",
    ]
    for entry in left_out:
        listing = entry.listing
        if entry.override and entry.override.include is False:
            reason = f"Excluded by hand: {entry.override.note}"
        else:
            reason = "No own drug or biological trial found"
        link = EDGAR_COMPANY_URL.format(cik=listing.cik)
        name = listing.name.replace("|", "/")
        lines.append(f"| {name} | {listing.ticker} | {entry.sic} | {reason} | [EDGAR]({link}) |")
    return "\n".join(lines) + "\n"
