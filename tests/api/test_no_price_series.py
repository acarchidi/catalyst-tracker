"""No API field carries prices, returns, ratings, or portfolio inputs (spec: data license; advice).

The API and MCP never return continuous price or return series, and never give buy/sell or
portfolio-specific answers. This walks every schema and every response for such field names.
"""

from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import TODAY, Seed
from catalyst.api.http import create_app

FORBIDDEN = {
    "price",
    "prices",
    "return",
    "returns",
    "ohlc",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "series",
    "rating",
    "recommendation",
    "portfolio",
    "holdings",
    "position",
    "buy",
    "sell",
}


def _names(node: Any) -> set[str]:
    """Every key in a JSON document, including the property names of a JSON schema."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():  # type: ignore[union-attr]
            found.add(str(key))  # type: ignore[arg-type]
            found |= _names(value)
    elif isinstance(node, list):
        for item in node:  # type: ignore[union-attr]
            found |= _names(item)
    return found


def _offending(names: set[str]) -> set[str]:
    return {n for n in names if FORBIDDEN & set(n.lower().replace("-", "_").split("_"))}


def test_the_guard_catches_price_series_fields() -> None:
    assert _offending({"close_price", "daily_returns", "ohlc", "window_start", "quote"}) == {
        "close_price",
        "daily_returns",
        "ohlc",
    }


def test_no_http_schema_or_response_field_names_a_price_series(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    app = create_app(session_factory, today=lambda: TODAY)
    assert _offending(_names(app.openapi()["components"])) == set()
    with TestClient(app) as client:
        for path in ["/v1/catalysts", f"/v1/catalysts/{seed.slipped}", "/v1/companies/EXB/catalysts"]:
            assert _offending(_names(client.get(path).json())) == set()
