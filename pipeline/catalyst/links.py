"""Check that stored source links still work (spec: Provenance, item 6; Gate 1).

Each link is fetched once, politely: only the five source sites, each at its own rate, each
site's robots.txt obeyed, redirects never followed. A link passes only when the site answers
200 at that exact address. ADR 0010 explains the verdicts.
"""

from __future__ import annotations

import random
import re
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from catalyst.collectors.http import HostPolicy, PoliteHttpClient, RobotsDisallowedError, shared_limiter
from catalyst.collectors.ratelimit import Limiter, NoWait
from catalyst.collectors.sec.edgar import SEC_LIMITER
from catalyst.db.tables import TABLES
from catalyst.schemas import SourceDocument

# Requests per second for each site that source links point to.
LINK_HOSTS = {
    "clinicaltrials.gov": 0.75,
    "www.accessdata.fda.gov": 1.0,
    "www.federalregister.gov": 1.0,
    "www.prnewswire.com": 0.5,
}


# Pages a site sends scripted visitors to instead of the document (ADR 0005, ADR 0010).
HUMAN_CHECK_HOSTS = frozenset({"unblock.federalregister.gov"})
_FEDREG_DOCUMENT = re.compile(
    r"^https://www\.federalregister\.gov/documents/\d{4}/\d{2}/\d{2}/(?P<number>[0-9A-Za-z-]+)/"
)
FEDREG_API = "https://www.federalregister.gov/api/v1/documents/{number}.json"


class LinkStatus(StrEnum):
    """One plain verdict per link."""

    OK = "works"
    MOVED = "moved"
    BROKEN = "missing"
    REFUSED = "refused by the site"
    FAILED = "site error or no answer"
    NOT_CHECKED = "not checked"


@dataclass(frozen=True)
class LinkResult:
    """The verdict for one link."""

    url: str
    status: LinkStatus
    detail: str = ""


class LinkClient:
    """Fetches source links politely and turns each answer into a verdict."""

    def __init__(self, http: PoliteHttpClient, api: PoliteHttpClient) -> None:
        """Wrap a polite client for source pages and one for documented APIs (ADR 0005)."""
        self._http = http
        self._api = api

    def check(self, url: str) -> LinkResult:
        """Fetch `url` once and return its verdict."""
        notice = _FEDREG_DOCUMENT.match(url)
        if notice:
            return self._check_notice(url, notice.group("number"))
        try:
            self._http.get(url)
        except RobotsDisallowedError:
            return LinkResult(url, LinkStatus.NOT_CHECKED, "the site's robots.txt does not allow checking it")
        except ValueError:
            return LinkResult(url, LinkStatus.NOT_CHECKED, "not one of the source sites")
        except httpx.HTTPStatusError as error:
            return _from_status(url, error.response)
        except httpx.HTTPError as error:
            return LinkResult(url, LinkStatus.FAILED, type(error).__name__)
        return LinkResult(url, LinkStatus.OK)

    def _check_notice(self, url: str, number: str) -> LinkResult:
        """Confirm a Federal Register notice through its API, which scripted requests may use.

        The notice pages send scripted visitors to a human check, so the API's own address for
        the document is compared with the stored one instead.
        """
        try:
            response = self._api.get(FEDREG_API.format(number=number), params={"fields[]": "html_url"})
        except httpx.HTTPStatusError as error:
            return _from_status(url, error.response)
        except httpx.HTTPError as error:
            return LinkResult(url, LinkStatus.FAILED, type(error).__name__)
        try:
            body: object = response.json()
        except ValueError:
            body = None
        current = body.get("html_url") if isinstance(body, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if not isinstance(current, str):
            return LinkResult(url, LinkStatus.FAILED, "the Federal Register API sent an unusable answer")
        if current == url:
            return LinkResult(url, LinkStatus.OK, "confirmed through the Federal Register API")
        return LinkResult(url, LinkStatus.MOVED, f"moved to {current}")


def _from_status(url: str, response: httpx.Response) -> LinkResult:
    code = response.status_code
    if 300 <= code < 400:
        location = str(response.headers.get("location", ""))
        host = urlsplit(location).hostname
        if host in HUMAN_CHECK_HOSTS:
            return LinkResult(url, LinkStatus.REFUSED, f"the site sent a human check ({location})")
        return LinkResult(url, LinkStatus.MOVED, f"moved to {location or '(no address)'}")
    if code in (404, 410):
        return LinkResult(url, LinkStatus.BROKEN, f"HTTP {code}")
    if code in (401, 403, 429):
        return LinkResult(url, LinkStatus.REFUSED, f"HTTP {code}")
    return LinkResult(url, LinkStatus.FAILED, f"HTTP {code}")


def link_client(
    contact_email: str, transport: httpx.BaseTransport | None = None, rate_limits: bool = True
) -> LinkClient:
    """Return a link client for the source sites; `rate_limits=False` is only for tests."""

    def limiter(host: str, per_second: float) -> Limiter:
        return shared_limiter(host, per_second) if rate_limits else NoWait()

    hosts = {host: HostPolicy(limiter(host, rate), check_robots=True) for host, rate in LINK_HOSTS.items()}
    hosts["www.sec.gov"] = HostPolicy(SEC_LIMITER if rate_limits else NoWait(), check_robots=True)
    # The Federal Register API is a documented public API, used under its own terms like the
    # collector uses it (ADR 0005); it shares the site's rate limit.
    fedreg = "www.federalregister.gov"
    api = {fedreg: HostPolicy(limiter(fedreg, LINK_HOSTS[fedreg]))}
    return LinkClient(
        PoliteHttpClient(contact_email, hosts, transport), PoliteHttpClient(contact_email, api, transport)
    )


@dataclass
class LinkReport:
    """Verdicts for the links checked, in the order checked."""

    results: list[LinkResult] = field(default_factory=list[LinkResult])

    def counts(self) -> dict[LinkStatus, int]:
        """Return how many links got each verdict."""
        return dict(Counter(result.status for result in self.results))

    @property
    def ok(self) -> bool:
        """True when every checked link works."""
        return all(result.status is LinkStatus.OK for result in self.results)


def check_links(
    session_factory: sessionmaker[Session], client: LinkClient, sample: int | None = None, seed: int = 0
) -> LinkReport:
    """Check every stored source link, or a repeatable random `sample` of them."""
    table = TABLES[SourceDocument]
    with session_factory() as session:
        urls = sorted(set(session.execute(select(table.c.url)).scalars()))
    if sample is not None and sample < len(urls):
        urls = sorted(random.Random(seed).sample(urls, sample))  # noqa: S311 - a repeatable sample, not security
    return LinkReport([client.check(url) for url in urls])


def links_markdown(report: LinkReport) -> str:
    """Return the report for the owner: totals, then every link that does not work."""
    working = report.counts().get(LinkStatus.OK, 0)
    lines = [
        "# Source link check",
        "",
        f"{working} of {len(report.results)} links work.",
        "",
        "| Verdict | Links |",
        "| --- | --- |",
        *(f"| {status.value} | {count} |" for status, count in sorted(report.counts().items())),
    ]
    problems = [result for result in report.results if result.status is not LinkStatus.OK]
    if problems:
        lines += ["", "| Link | Verdict | Detail |", "| --- | --- | --- |"]
        lines += [f"| {r.url} | {r.status.value} | {r.detail} |" for r in problems]
    return "\n".join(lines) + "\n"
