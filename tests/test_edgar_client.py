"""The EDGAR client identifies itself, stays on SEC hosts, and stops on errors (spec: Data sources)."""

import httpx
import pytest

from catalyst.collectors.ratelimit import MinIntervalLimiter
from catalyst.collectors.sec.edgar import EdgarClient


class _NoWait(MinIntervalLimiter):
    def __init__(self) -> None:
        super().__init__(max_per_second=8)
        self.waits = 0

    def wait(self) -> None:
        self.waits += 1


def _client(handler: httpx.MockTransport) -> tuple[EdgarClient, _NoWait]:
    limiter = _NoWait()
    return EdgarClient("data@example.org", handler, limiter), limiter


def test_every_request_declares_a_user_agent_with_the_contact_email() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"{}")

    client, _ = _client(httpx.MockTransport(handler))
    client.get("https://data.sec.gov/submissions/CIK0000000001.json")
    assert seen[0].headers["User-Agent"] == "catalyst-tracker data@example.org"


def test_every_request_passes_through_the_rate_limiter() -> None:
    client, limiter = _client(httpx.MockTransport(lambda _: httpx.Response(200)))
    for _ in range(3):
        client.get("https://www.sec.gov/Archives/edgar/data/1/2/x.htm")
    assert limiter.waits == 3


@pytest.mark.parametrize(
    "url",
    ["https://example.org/x", "https://sec.gov.evil.example/x", "https://www.sec.gov.example.org/x"],
)
def test_non_sec_hosts_are_refused_before_any_request(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent")

    client, _ = _client(httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="SEC hosts"):
        client.get(url)


@pytest.mark.parametrize("status", [403, 404, 429, 500])
def test_error_statuses_raise(status: int) -> None:
    client, _ = _client(httpx.MockTransport(lambda _: httpx.Response(status)))
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://www.sec.gov/Archives/edgar/data/1/2/x.htm")


def test_redirects_are_not_followed_off_sec() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "www.sec.gov":
            return httpx.Response(302, headers={"location": "https://example.org/elsewhere"})
        return httpx.Response(200, content=b"not from the SEC")

    client, _ = _client(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://www.sec.gov/Archives/edgar/data/1/2/x.htm")
    assert seen == ["https://www.sec.gov/Archives/edgar/data/1/2/x.htm"]


def test_clients_share_one_rate_limiter_by_default() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(200))
    first = EdgarClient("data@example.org", transport)
    second = EdgarClient("data@example.org", transport)
    assert first._hosts["www.sec.gov"].limiter is second._hosts["data.sec.gov"].limiter  # pyright: ignore[reportPrivateUsage]
