"""Read-only HTTP API under /v1 (spec: Machine access; ADR 0025). Runs on localhost only for now."""

from collections.abc import Callable, Generator, Sequence
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException as StarletteHTTPException

from catalyst.api import read
from catalyst.api.access import NOTICE, Access
from catalyst.api.views import CatalystPage, CatalystResult, CompanyCatalystPage
from catalyst.schemas import CatalystType

NEW_YORK = ZoneInfo("America/New_York")


def new_york_today() -> date:
    """Return today's date in New York, where the product's day starts and ends."""
    return datetime.now(NEW_YORK).date()


def access_level() -> Access:
    """Return the caller's access level: free for everyone until accounts exist (roadmap Phase 3)."""
    return Access.FREE


def _session(request: Request) -> Generator[Session]:
    with read.read_session(request.app.state.session_factory) as session:
        yield session


def _today(request: Request) -> date:
    today: date = request.app.state.today()
    return today


Db = Annotated[Session, Depends(_session)]
Level = Annotated[Access, Depends(access_level)]
Today = Annotated[date, Depends(_today)]
Start = Annotated[date | None, Query(alias="from", description="First day of the date window.")]
End = Annotated[date | None, Query(alias="to", description="Last day of the date window.")]
Types = Annotated[list[CatalystType] | None, Query(alias="type", description="Catalyst types.")]
Cursor = Annotated[str | None, Query(description="The `next_cursor` of the previous page.")]
Limit = Annotated[int, Query(ge=1, le=read.MAX_LIMIT, description="Most catalysts per page.")]
CompanyKey = Annotated[str | None, Query(description="Company id, SEC number, or ticker.")]
DrugKey = Annotated[str | None, Query(description="Drug id or any of its names.")]

router = APIRouter(prefix="/v1")


@router.get("/catalysts", summary="Upcoming catalysts in a date window")
def list_catalysts(
    db: Db,
    access: Level,
    today: Today,
    start: Start = None,
    end: End = None,
    company: CompanyKey = None,
    drug: DrugKey = None,
    types: Types = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> CatalystPage:
    """List upcoming catalysts whose window overlaps the date window, earliest first."""
    query = read.Query(start, end, company, drug, tuple(types or ()), cursor, limit)
    return read.upcoming(db, query, access, today)


@router.get("/catalysts/{catalyst_id}", summary="One catalyst with its sources")
def get_catalyst(catalyst_id: UUID, db: Db, access: Level) -> CatalystResult:
    """Return one published catalyst."""
    found = read.catalyst(db, catalyst_id, access)
    if found is None:
        raise HTTPException(404, "No published catalyst has this id.")
    return found


@router.get("/companies/{company}/catalysts", summary="A company's upcoming catalysts")
def list_company_catalysts(
    company: str,
    db: Db,
    access: Level,
    today: Today,
    start: Start = None,
    end: End = None,
    types: Types = None,
    cursor: Cursor = None,
    limit: Limit = 50,
) -> CompanyCatalystPage:
    """List one company's upcoming catalysts; the company is an id, SEC number, or ticker."""
    query = read.Query(start, end, None, None, tuple(types or ()), cursor, limit)
    found = read.company_catalysts(db, company, query, access, today)
    if found is None:
        raise HTTPException(404, "No covered company has this id, SEC number, or ticker.")
    return found


def _error(status: int, detail: object) -> JSONResponse:
    return JSONResponse({"detail": detail, "notice": NOTICE}, status_code=status)


async def _http_error(_request: Request, error: Exception) -> JSONResponse:
    if isinstance(error, StarletteHTTPException):
        return _error(error.status_code, error.detail)
    return _error(500, "error")  # pragma: no cover - registered for HTTP errors only


async def _invalid(_request: Request, error: Exception) -> JSONResponse:
    problems: Sequence[Any] = error.errors() if isinstance(error, RequestValidationError) else ()
    return _error(422, [{"loc": p["loc"], "msg": p["msg"]} for p in problems])


async def _bad_query(_request: Request, error: Exception) -> JSONResponse:
    return _error(400, str(error))


def create_app(session_factory: sessionmaker[Session], today: Callable[[], date] = new_york_today) -> FastAPI:
    """Build the API over the read layer. Every route is GET; every response carries the notice."""
    app = FastAPI(
        title="Catalyst tracker API",
        version="1.0.0",
        description=f"Read-only biotech catalyst data, every value with its source. {NOTICE}",
    )
    app.state.session_factory = session_factory
    app.state.today = today
    app.include_router(router)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _invalid)
    app.add_exception_handler(read.InvalidQueryError, _bad_query)
    return app
