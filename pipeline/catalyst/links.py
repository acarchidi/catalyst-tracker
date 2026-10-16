"""Check that stored source links still work (spec: Provenance, item 6; Gate 1).

Each link is fetched once, politely: only the five source sites, each at its own rate, each
site's robots.txt obeyed, redirects never followed. A link passes only when the site answers
200 at that exact address. ADR 0010 explains the verdicts.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

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

    def __init__(self, http: PoliteHttpClient) -> None:
        """Wrap a polite client limited to the source sites."""
        self._http = http

    def check(self, url: str) -> LinkResult:
        """Fetch `url` once and return its verdict."""
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


def _from_status(url: str, response: httpx.Response) -> LinkResult:
    code = response.status_code
    if 300 <= code < 400:
        return LinkResult(
            url, LinkStatus.MOVED, f"moved to {response.headers.get('location', '(no address)')}"
        )
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
    return LinkClient(PoliteHttpClient(contact_email, hosts, transport))


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
