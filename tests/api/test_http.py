"""The read-only HTTP API under /v1: free by default, every response carries the notice."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, tzinfo
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import TODAY, Seed
from catalyst.api import http
from catalyst.api.access import NOTICE, Access
from catalyst.api.http import access_level, create_app, new_york_today


@pytest.fixture
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    with TestClient(create_app(session_factory, today=lambda: TODAY)) as test_client:
        yield test_client


@pytest.fixture
def paid(client: TestClient) -> TestClient:
    client.app.dependency_overrides[access_level] = lambda: Access.PAID  # type: ignore[attr-defined]
    return client


def _ids(body: dict[str, Any]) -> list[str]:
    return [c["id"] for c in body["catalysts"]]


def test_calendar_is_free_by_default(client: TestClient, seed: Seed) -> None:
    response = client.get("/v1/catalysts")
    assert response.status_code == 200
    body = response.json()
    assert body["notice"] == NOTICE
    assert body["access"] == "free"
    assert _ids(body) == [str(seed.slipped), str(seed.adcom)]
    assert all("history" not in c for c in body["catalysts"])
    assert all(c["sources"] for c in body["catalysts"])


def test_paid_caller_gets_full_horizon_and_history(paid: TestClient, seed: Seed) -> None:
    body = paid.get("/v1/catalysts").json()
    assert _ids(body) == [str(seed.slipped), str(seed.adcom), str(seed.pdufa)]
    assert all("history" in c for c in body["catalysts"])


def test_filters_and_paging(paid: TestClient, seed: Seed) -> None:
    body = paid.get("/v1/catalysts", params={"company": "EXB", "type": ["pdufa_date"]}).json()
    assert _ids(body) == [str(seed.pdufa)]
    body = paid.get(
        "/v1/catalysts", params={"drug": "EXB-101", "from": "2026-10-01", "to": "2026-12-31"}
    ).json()
    assert _ids(body) == [str(seed.slipped)]
    first = paid.get("/v1/catalysts", params={"limit": 2}).json()
    rest = paid.get("/v1/catalysts", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert _ids(first) + _ids(rest) == [str(seed.slipped), str(seed.adcom), str(seed.pdufa)]
    assert rest["next_cursor"] is None


def test_one_catalyst(client: TestClient, seed: Seed) -> None:
    body = client.get(f"/v1/catalysts/{seed.slipped}").json()
    assert body["notice"] == NOTICE
    assert body["catalyst"]["window"]["raw_text"] == "4Q26"
    assert "history" not in body["catalyst"]


def test_company_catalysts(client: TestClient, seed: Seed) -> None:
    body = client.get("/v1/companies/OTH/catalysts").json()
    assert body["company"]["cik"] == 2222
    assert _ids(body) == [str(seed.adcom)]


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/v1/catalysts/00000000-0000-0000-0000-000000000000", 404),
        ("/v1/companies/NOPE/catalysts", 404),
        ("/v1/catalysts/not-an-id", 422),
        ("/v1/catalysts?cursor=bad", 400),
        ("/v1/catalysts?limit=500", 422),
        ("/v1/nothing-here", 404),
        ("/v1/companies/%C2%B2/catalysts", 404),
        ("/v1/catalysts?company=%C2%B2", 200),
        ("/v1/companies/99999999999999999999999/catalysts", 404),
        ("/v1/catalysts?company=99999999999999999999999", 200),
    ],
)
def test_errors_carry_the_notice(client: TestClient, seed: Seed, path: str, status: int) -> None:
    response = client.get(path)
    assert response.status_code == status
    assert response.json()["notice"] == NOTICE


def test_only_reads_under_v1(client: TestClient) -> None:
    paths: dict[str, dict[str, Any]] = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/v1/catalysts",
        "/v1/catalysts/{catalyst_id}",
        "/v1/companies/{company}/catalysts",
    }
    assert all(set(operations) == {"get"} for operations in paths.values())
    assert client.post("/v1/catalysts").status_code == 405


def test_a_server_error_carries_the_notice(session_factory: sessionmaker[Session]) -> None:
    app = create_app(session_factory, today=lambda: TODAY)

    @app.get("/v1/broken")
    def broken() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("a bug")

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/v1/broken")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error.", "notice": NOTICE}


class _LateEvening(datetime):
    """23:30 in New York on 2026-10-17, which is already 03:30 on 2026-10-18 in UTC."""

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> "_LateEvening":
        instant = datetime(2026, 10, 18, 3, 30, tzinfo=UTC)
        moment = instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
        return cls.fromisoformat(moment.isoformat())


def test_the_day_is_new_yorks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(http, "datetime", _LateEvening)
    assert new_york_today() == date(2026, 10, 17)
