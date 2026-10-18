"""`catalyst api` serves on this computer only; `catalyst mcp` speaks MCP over stdio."""

from typing import Any

import pytest
from fastapi import FastAPI
from mcp.server.mcpserver import MCPServer

from catalyst import cli


def test_api_listens_on_localhost_only(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    seen: dict[str, Any] = {}

    def run(app: Any, **options: Any) -> None:
        seen.update(options, app=app)

    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    assert cli.main(["api", "--port", "8123"]) == 0
    assert isinstance(seen["app"], FastAPI)
    assert (seen["host"], seen["port"]) == ("127.0.0.1", 8123)


def test_mcp_runs_over_stdio(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    transports: list[str] = []
    monkeypatch.setenv("CATALYST_DATABASE_URL", database_url)
    monkeypatch.setattr(MCPServer, "run", lambda _self, transport: transports.append(transport))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType, reportUnknownMemberType]
    assert cli.main(["mcp"]) == 0
    assert transports == ["stdio"]


@pytest.mark.parametrize("command", ["api", "mcp"])
def test_an_unusable_database_setting_names_the_setup_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str
) -> None:
    monkeypatch.delenv("CATALYST_DATABASE_URL", raising=False)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://postgres@db.example.supabase.co:5432/postgres")
    assert cli.main([command]) == 2
    assert "docs/setup.md" in capsys.readouterr().err
