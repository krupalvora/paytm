from fastapi import APIRouter, Depends, Request

from app import shows
from app.auth import require_admin
from app.errors import AppError
from app.schemas import CreateShowRequest, ShowView

router = APIRouter(tags=["shows"])


@router.post("/shows", status_code=201, response_model=ShowView, dependencies=[Depends(require_admin)])
async def create_show(body: CreateShowRequest, request: Request) -> ShowView:
    settings = request.app.state.settings
    if len(body.seats) > settings.max_seats_per_show:
        raise AppError(422, "too_many_seats", f"a show may have at most {settings.max_seats_per_show} seats")

    async with request.app.state.pool.acquire(timeout=settings.db_acquire_timeout_s) as conn:
        show_id = await shows.create_show(
            conn,
            body,
            per_user_limit=body.per_user_limit or settings.default_per_user_limit,
            hold_ttl_seconds=body.hold_ttl_seconds or settings.default_hold_ttl_seconds,
        )
        return await shows.get_show(conn, show_id)


@router.get("/shows/{show_id}", response_model=ShowView)
async def get_show(show_id: str, request: Request) -> ShowView:
    sid = shows.parse_show_id(show_id)
    async with request.app.state.pool.acquire(timeout=request.app.state.settings.db_acquire_timeout_s) as conn:
        return await shows.get_show(conn, sid)
