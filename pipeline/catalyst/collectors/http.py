"""Polite HTTP for every collector: allowed hosts only, a rate limit per host, robots.txt where it applies.

Documented public APIs (SEC EDGAR, ClinicalTrials.gov, openFDA, Federal Register) follow their own
access policies; web pages and feeds also obey robots.txt (ADR 0005).
"""

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from catalyst.collectors.ratelimit import Limiter, MinIntervalLimiter
from catalyst.logs import log_event

BOT_NAME = "catalyst-tracker"
TIMEOUT_SECONDS = 30

_log = logging.getLogger(__name__)
_limiters: dict[str, MinIntervalLimiter] = {}
_limiters_lock = threading.Lock()


def shared_limiter(host: str, max_per_second: float) -> MinIntervalLimiter:
    """Return the one limiter for `host` in this process, so every client shares its rate."""
    with _limiters_lock:
        if host not in _limiters:
            _limiters[host] = MinIntervalLimiter(max_per_second)
        return _limiters[host]


class RobotsDisallowedError(PermissionError):
    """The site's robots.txt does not allow this URL."""


@dataclass(frozen=True)
class HostPolicy:
    """How one host may be fetched."""

    limiter: Limiter
    check_robots: bool = False


class PoliteHttpClient:
    """GET requests to a fixed set of hosts, each with its own rate limit; redirects are never followed."""

    host_label = "its configured hosts"

    def __init__(
        self,
        contact_email: str,
        hosts: Mapping[str, HostPolicy],
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Create a client that identifies itself with `contact_email` on every request."""
        self._hosts = dict(hosts)
        self._http = httpx.Client(transport=transport, timeout=TIMEOUT_SECONDS, follow_redirects=False)
        self._headers = {
            "User-Agent": f"{BOT_NAME} {contact_email}",
            "Accept-Encoding": "gzip, deflate",
        }
        self._robots: dict[str, RobotFileParser] = {}

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> httpx.Response:
        """Fetch `url`, raising for a disallowed host or path, or any non-success status."""
        host = urlsplit(url).hostname or ""
        policy = self._hosts.get(host)
        if policy is None:
            raise ValueError(f"this client only fetches from {self.host_label}, not {host!r}")
        if policy.check_robots and not self._robots_allow(host, policy, url):
            raise RobotsDisallowedError(f"robots.txt on {host} does not allow {url}")
        policy.limiter.wait()
        response = self._http.get(url, params=params, headers=self._headers)
        log_event(
            _log,
            "http_request",
            url=str(response.url),
            status=response.status_code,
            bytes=len(response.content),
        )
        response.raise_for_status()
        return response

    def _robots_allow(self, host: str, policy: HostPolicy, url: str) -> bool:
        if host not in self._robots:
            policy.limiter.wait()
            response = self._http.get(f"https://{host}/robots.txt", headers=self._headers)
            parser = RobotFileParser()
            if response.status_code == 200:
                parser.parse(response.text.splitlines())
            elif 400 <= response.status_code < 500 and response.status_code not in (401, 403, 429):
                parser.parse([])  # no robots.txt means no restrictions
            else:
                parser.parse(["User-agent: *", "Disallow: /"])  # refused or unreadable: fetch nothing
            self._robots[host] = parser
        return self._robots[host].can_fetch(BOT_NAME, url)
