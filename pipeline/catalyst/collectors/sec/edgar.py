"""Read EDGAR's company filing list and filing index pages, and fetch documents politely.

EDGAR rules (sec.gov/os/accessing-edgar-data): declare a User-Agent with a contact
email, and send no more than 10 requests per second.
"""

import logging
from datetime import date
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from catalyst.collectors.ratelimit import MinIntervalLimiter
from catalyst.logs import log_event

SEC_HOSTS = frozenset({"www.sec.gov", "data.sec.gov"})
SEC_WEB_ROOT = "https://www.sec.gov"
ARCHIVES_URL = f"{SEC_WEB_ROOT}/Archives/edgar/data"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
PRESS_RELEASE_EXHIBITS = ("EX-99.1", "EX-99")
REQUESTS_PER_SECOND = 8
TIMEOUT_SECONDS = 30

_log = logging.getLogger(__name__)


class FilingRef(BaseModel):
    """One filing from a company's EDGAR filing list."""

    model_config = ConfigDict(frozen=True)

    cik: int
    accession_number: str
    form: str
    filed_on: date
    accepted_at: AwareDatetime
    primary_document: str

    @property
    def folder_url(self) -> str:
        """The filing's folder in the EDGAR archive."""
        return f"{ARCHIVES_URL}/{self.cik}/{self.accession_number.replace('-', '')}"

    @property
    def index_url(self) -> str:
        """The filing's index page, which lists every document and its exhibit type."""
        return f"{self.folder_url}/{self.accession_number}-index.htm"


class ExhibitRef(BaseModel):
    """One document listed on a filing index page."""

    model_config = ConfigDict(frozen=True)

    type: str
    description: str
    url: str


class _RecentFilings(BaseModel):
    accession_number: list[str] = Field(alias="accessionNumber")
    filing_date: list[date] = Field(alias="filingDate")
    acceptance_date_time: list[AwareDatetime] = Field(alias="acceptanceDateTime")
    form: list[str]
    primary_document: list[str] = Field(alias="primaryDocument")


class _Filings(BaseModel):
    recent: _RecentFilings


class _Submissions(BaseModel):
    cik: int
    filings: _Filings


def recent_filings(submissions_json: bytes, form: str) -> list[FilingRef]:
    """Return the company's recent filings of exactly `form` (amendments excluded), newest first."""
    data = _Submissions.model_validate_json(submissions_json)
    recent = data.filings.recent
    rows = zip(
        recent.accession_number,
        recent.filing_date,
        recent.acceptance_date_time,
        recent.form,
        recent.primary_document,
        strict=True,
    )
    filings = [
        FilingRef(
            cik=data.cik,
            accession_number=accession,
            form=row_form,
            filed_on=filed_on,
            accepted_at=accepted_at,
            primary_document=primary,
        )
        for accession, filed_on, accepted_at, row_form, primary in rows
        if row_form == form
    ]
    return sorted(filings, key=lambda filing: filing.accepted_at, reverse=True)


class _DocumentTableParser(HTMLParser):
    """Collect rows of the 'Document Format Files' table on an EDGAR filing index page."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[list[str], str | None]] = []
        self._tables_seen = 0
        self._in_table = False
        self._cells: list[str] | None = None
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "table" and "tableFile" in (attributes.get("class") or "").split():
            self._tables_seen += 1
            self._in_table = self._tables_seen == 1
        elif self._in_table and tag == "tr":
            self._cells, self._href = [], None
        elif self._in_table and tag == "td":
            self._text = []
        elif self._in_table and tag == "a" and self._cells is not None and self._href is None:
            self._href = attributes.get("href")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_table:
            return
        if tag == "td" and self._cells is not None:
            self._cells.append(" ".join("".join(self._text).split()))
        elif tag == "tr" and self._cells:
            self.rows.append((self._cells, self._href))
            self._cells = None
        elif tag == "table":
            self._in_table = False

    def handle_data(self, data: str) -> None:
        if self._in_table:
            self._text.append(data)


def _document_url(href: str) -> str:
    """Turn an index-page link into a direct https link, unwrapping the inline-XBRL viewer."""
    path = href.removeprefix("/ix?doc=")
    return urljoin(SEC_WEB_ROOT + "/", path)


def list_documents(index_html: str) -> list[ExhibitRef]:
    """Return every document in the filing's 'Document Format Files' table."""
    parser = _DocumentTableParser()
    parser.feed(index_html)
    documents: list[ExhibitRef] = []
    for cells, href in parser.rows:
        # Columns: Seq, Description, Document, Type, Size.
        if href is None or len(cells) < 4:
            continue
        documents.append(ExhibitRef(type=cells[3], description=cells[1], url=_document_url(href)))
    return documents


def find_press_release(index_html: str) -> ExhibitRef | None:
    """Return the filing's press-release exhibit (EX-99.1, else EX-99), or None if it has none."""
    documents = list_documents(index_html)
    for exhibit_type in PRESS_RELEASE_EXHIBITS:
        for document in documents:
            if document.type == exhibit_type:
                return document
    return None


class EdgarClient:
    """HTTP client for SEC hosts only: declared User-Agent, rate limit, and a log line per request."""

    def __init__(
        self,
        contact_email: str,
        http: httpx.Client | None = None,
        limiter: MinIntervalLimiter | None = None,
    ) -> None:
        """Create a client that identifies itself with `contact_email`, as the SEC requires."""
        self._http = http or httpx.Client(timeout=TIMEOUT_SECONDS, follow_redirects=False)
        self._limiter = limiter or MinIntervalLimiter(REQUESTS_PER_SECOND)
        self._headers = {
            "User-Agent": f"catalyst-tracker {contact_email}",
            "Accept-Encoding": "gzip, deflate",
        }

    def get(self, url: str) -> httpx.Response:
        """Fetch `url` from an SEC host, raising for any non-success status."""
        host = urlsplit(url).hostname
        if host not in SEC_HOSTS:
            raise ValueError(f"EdgarClient only fetches from SEC hosts, not {host!r}")
        self._limiter.wait()
        response = self._http.get(url, headers=self._headers)
        log_event(_log, "sec_request", url=url, status=response.status_code, bytes=len(response.content))
        response.raise_for_status()
        return response
