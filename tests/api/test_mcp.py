"""The read-only MCP server: three tools over the same read layer, each carrying the notice."""

from typing import Any, cast

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import TODAY, Seed
from catalyst.api.access import NOTICE
from catalyst.api.mcp_server import build_server

TOOLS = {"list_upcoming_catalysts", "get_catalyst", "list_company_catalysts"}


def _call(session_factory: sessionmaker[Session], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    server = build_server(session_factory, today=lambda: TODAY)
    result = anyio.run(server.call_tool, name, arguments)
    assert isinstance(result, CallToolResult)
    return cast(dict[str, Any], result.structured_content)


def test_three_read_only_tools_each_carry_the_notice(session_factory: sessionmaker[Session]) -> None:
    tools = anyio.run(build_server(session_factory).list_tools)
    assert {t.name for t in tools} == TOOLS
    for tool in tools:
        assert tool.description is not None
        assert NOTICE in tool.description
        assert "never returns price or return series" in tool.description.lower()
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.output_schema is not None


def test_list_upcoming_catalysts_is_the_free_calendar(
    session_factory: sessionmaker[Session], seed: Seed
) -> None:
    body = _call(session_factory, "list_upcoming_catalysts", {})
    assert body["notice"] == NOTICE
    assert [c["id"] for c in body["catalysts"]] == [str(seed.slipped), str(seed.adcom)]
    assert all("history" not in c for c in body["catalysts"])
    body = _call(session_factory, "list_upcoming_catalysts", {"catalyst_types": ["advisory_committee"]})
    assert [c["id"] for c in body["catalysts"]] == [str(seed.adcom)]


def test_get_catalyst_and_company_catalysts(session_factory: sessionmaker[Session], seed: Seed) -> None:
    body = _call(session_factory, "get_catalyst", {"catalyst_id": str(seed.slipped)})
    assert body["notice"] == NOTICE
    assert body["catalyst"]["sources"]
    body = _call(session_factory, "list_company_catalysts", {"company": "EXB"})
    assert body["notice"] == NOTICE
    assert body["company"]["ticker"] == "EXB"
    assert [c["id"] for c in body["catalysts"]] == [str(seed.slipped)]


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_catalyst", {"catalyst_id": "00000000-0000-0000-0000-000000000000"}),
        ("get_catalyst", {"catalyst_id": "not-an-id"}),
        ("list_company_catalysts", {"company": "NOPE"}),
        ("list_upcoming_catalysts", {"cursor": "bad"}),
        ("list_company_catalysts", {"company": "EXB", "limit": 0}),
    ],
)
def test_tool_errors_carry_the_notice(
    session_factory: sessionmaker[Session], seed: Seed, name: str, arguments: dict[str, Any]
) -> None:
    with pytest.raises(ToolError, match="financial advice"):
        _call(session_factory, name, arguments)
