"""The read-only MCP server: three tools over the same read layer, each carrying the notice."""

import inspect
from datetime import date, timedelta
from typing import Any, cast

import anyio
import pytest
from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from sqlalchemy import create_engine, insert
from sqlalchemy.orm import Session, sessionmaker

from api.conftest import TODAY, UNPUBLISHED_QUOTE, Seed
from catalyst import cli
from catalyst.api.access import NOTICE
from catalyst.api.mcp_server import build_server
from catalyst.db.tables import TABLES
from catalyst.schemas import CatalystDrug, Drug, DrugRole

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


def _over_the_wire(server: MCPServer, name: str, arguments: dict[str, Any]) -> CallToolResult:
    """Call a tool the way an MCP client does, so the SDK's own error handling is included."""

    async def call() -> CallToolResult:
        async with Client(server) as client:
            return await client.call_tool(name, arguments)

    return anyio.run(call)


def _text(result: CallToolResult) -> str:
    return " ".join(c.text for c in result.content if isinstance(c, TextContent))


# A database that cannot be reached; its address and password must never reach the caller.
_DOWN = "postgresql+psycopg://reader:hunter2-not-a-real-password@127.0.0.1:1/catalyst"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_catalyst", {}),
        ("get_catalyst", {"catalyst_id": "not-an-id"}),
        ("list_upcoming_catalysts", {"start": "soon"}),
        ("list_upcoming_catalysts", {"limit": "many"}),
        ("list_upcoming_catalysts", {"catalyst_types": ["price_target"]}),
        ("list_company_catalysts", {"company": "NOPE"}),
        ("no_such_tool", {}),
    ],
)
def test_every_error_a_client_sees_carries_the_notice(
    session_factory: sessionmaker[Session], seed: Seed, name: str, arguments: dict[str, Any]
) -> None:
    result = _over_the_wire(build_server(session_factory, today=lambda: TODAY), name, arguments)
    assert result.is_error
    assert NOTICE in _text(result)


def test_a_crash_shows_only_the_notice_and_no_detail() -> None:
    engine = create_engine(_DOWN)
    try:
        server = build_server(sessionmaker(engine), today=lambda: TODAY)
        for name, arguments in [
            ("list_upcoming_catalysts", {}),
            ("get_catalyst", {"catalyst_id": "00000000-0000-0000-0000-000000000000"}),
            ("list_company_catalysts", {"company": "EXB"}),
        ]:
            result = _over_the_wire(server, name, arguments)
            assert result.is_error
            text = _text(result)
            assert NOTICE in text
            for detail in ("hunter2", "127.0.0.1", "reader", "psycopg", "connection"):
                assert detail not in text.lower()
    finally:
        engine.dispose()


def test_a_held_back_catalyst_stays_hidden(session_factory: sessionmaker[Session], seed: Seed) -> None:
    # A drug link added by hand, citing a claim no published change cites: the read layer holds
    # the whole catalyst back (PR #41), and the MCP tools must too.
    with session_factory.begin() as session:
        drug = Drug(company_id=seed.example, preferred_name="secondumab")
        session.execute(insert(TABLES[Drug]).values(**drug.model_dump()))
        link = CatalystDrug(
            catalyst_id=seed.slipped, drug_id=drug.id, role=DrugRole.ALONE, claim_id=seed.unpublished_claim
        )
        session.execute(insert(TABLES[CatalystDrug]).values(**link.model_dump()))
    server = build_server(session_factory, today=lambda: TODAY)
    for name, arguments in [
        ("list_upcoming_catalysts", {}),
        ("list_upcoming_catalysts", {"drug": "secondumab"}),
        ("list_company_catalysts", {"company": "EXB"}),
    ]:
        result = _over_the_wire(server, name, arguments)
        assert not result.is_error
        assert str(seed.slipped) not in result.model_dump_json()
        assert UNPUBLISHED_QUOTE not in result.model_dump_json()
    held = _over_the_wire(server, "get_catalyst", {"catalyst_id": str(seed.slipped)})
    assert held.is_error
    assert "No published catalyst" in _text(held)
    assert UNPUBLISHED_QUOTE not in held.model_dump_json()


def test_catalyst_mcp_is_always_free(
    monkeypatch: pytest.MonkeyPatch, database_url: str, session_factory: sessionmaker[Session], seed: Seed
) -> None:
    assert "access" not in inspect.signature(build_server).parameters
    servers: list[MCPServer] = []
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    monkeypatch.setattr(MCPServer, "run", lambda self, _transport: servers.append(self))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType, reportUnknownMemberType]
    assert cli.main(["mcp"]) == 0
    (server,) = servers
    far = _over_the_wire(server, "list_upcoming_catalysts", {"end": "2027-06-30"})
    page = cast(dict[str, Any], far.structured_content)
    assert page["access"] == "free"
    first, last = date.fromisoformat(page["window_from"]), date.fromisoformat(page["window_to"])
    assert last - first == timedelta(days=30)
    one = _over_the_wire(server, "get_catalyst", {"catalyst_id": str(seed.slipped)})
    found = cast(dict[str, Any], one.structured_content)
    assert found["access"] == "free"
    assert "history" not in found["catalyst"]
