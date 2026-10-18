"""Read-only MCP server over the same read layer as the HTTP API (spec: Machine access; ADR 0025).

It runs over stdio on the caller's machine and is always free tier (ADR 0025, rule 6).
"""

from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import UUID

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, ToolAnnotations
from sqlalchemy.orm import Session, sessionmaker

from catalyst.api import read
from catalyst.api.access import NOTICE, Access
from catalyst.api.http import new_york_today
from catalyst.api.views import CatalystPage, CatalystResult, CompanyCatalystPage
from catalyst.schemas import CatalystType

RULES = (
    "Read-only. Every value comes with `sources`: a deep link to the primary document and the "
    "verbatim quote that states it. Dates are windows (start, end, precision, the source's own "
    "words, and who stands behind the date), not single days. This tool never returns price or "
    "return series, buy or sell ratings, price targets, or answers about anyone's holdings. "
)
FILTERS = (
    "Inputs: `start` and `end` (dates as YYYY-MM-DD; the free tier shows today through today + 30 "
    "days), `catalyst_types`, and `cursor` with `limit` (1 to 200) for paging; pass the "
    "`next_cursor` of one page to get the next."
)
_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


def _refuse(message: str) -> ToolError:
    return ToolError(f"{message} {NOTICE}")


class _NoticedServer(MCPServer[Any]):
    """An MCP server whose every tool error ends with the notice (ADR 0025, rule 7).

    That includes the SDK's own errors: an unknown tool, arguments that fail the input schema, and
    a crash. A crash still shows only "Error executing tool <name>"; its detail goes to the log.
    """

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: Context[Any, Any] | None = None
    ) -> CallToolResult | InputRequiredResult:
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as error:
            if NOTICE in str(error):
                raise
            # Same type and cause, so the SDK logs it as before: field names only for bad arguments.
            raise type(error)(f"{error}. {NOTICE}") from error.__cause__


def build_server(
    session_factory: sessionmaker[Session], today: Callable[[], date] = new_york_today
) -> MCPServer[Any]:
    """Build the server with its three read-only tools. Over stdio every caller is free tier."""
    access = Access.FREE
    server = _NoticedServer(name="catalyst-tracker", instructions=f"Biotech catalyst data. {RULES}{NOTICE}")

    @server.tool(
        description=(
            "List upcoming biotech catalysts (FDA decision dates, advisory committees, trial readouts, "
            "and other events) whose date window overlaps the window you give, earliest first. "
            f"Also filters by `company` (internal id, SEC CIK, or ticker) and `drug` (id or any name). "
            f"{FILTERS} {RULES}{NOTICE}"
        ),
        annotations=_READ_ONLY,
    )
    def list_upcoming_catalysts(  # pyright: ignore[reportUnusedFunction]
        start: date | None = None,
        end: date | None = None,
        company: str | None = None,
        drug: str | None = None,
        catalyst_types: list[CatalystType] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> CatalystPage:
        try:
            query = read.Query(start, end, company, drug, tuple(catalyst_types or ()), cursor, limit)
            with read.read_session(session_factory) as session:
                return read.upcoming(session, query, access, today())
        except read.InvalidQueryError as error:
            raise _refuse(f"{error}.") from error

    @server.tool(
        description=(
            "Get one catalyst by its id, with every source and quote, its date confidence, and its "
            f"latest date change. {RULES}{NOTICE}"
        ),
        annotations=_READ_ONLY,
    )
    def get_catalyst(catalyst_id: str) -> CatalystResult:  # pyright: ignore[reportUnusedFunction]
        try:
            wanted = UUID(catalyst_id)
        except ValueError as error:
            raise _refuse("catalyst_id must be a catalyst id from a list of catalysts.") from error
        with read.read_session(session_factory) as session:
            found = read.catalyst(session, wanted, access)
        if found is None:
            raise _refuse("No published catalyst has this id.")
        return found

    @server.tool(
        description=(
            "List one company's upcoming catalysts. `company` is the internal id, SEC CIK, or ticker. "
            f"{FILTERS} {RULES}{NOTICE}"
        ),
        annotations=_READ_ONLY,
    )
    def list_company_catalysts(  # pyright: ignore[reportUnusedFunction]
        company: str,
        start: date | None = None,
        end: date | None = None,
        catalyst_types: list[CatalystType] | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> CompanyCatalystPage:
        try:
            query = read.Query(start, end, None, None, tuple(catalyst_types or ()), cursor, limit)
            with read.read_session(session_factory) as session:
                found = read.company_catalysts(session, company, query, access, today())
        except read.InvalidQueryError as error:
            raise _refuse(f"{error}.") from error
        if found is None:
            raise _refuse("No covered company has this id, SEC number, or ticker.")
        return found

    return server
