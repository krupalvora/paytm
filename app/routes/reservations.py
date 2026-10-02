import re

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import reservations, shows
from app.auth import current_user
from app.errors import AppError
from app.schemas import IDEMPOTENCY_KEY_PATTERN, ReserveRequest

router = APIRouter(tags=["reservations"])

_TRANSIENT = (asyncpg.DeadlockDetectedError, asyncpg.SerializationError)


def _idempotency_key(request: Request, body: ReserveRequest) -> str:
    header = request.headers.get("idempotency-key")
    if header is not None and body.idempotency_key is not None and header != body.idempotency_key:
        raise AppError(422, "idempotency_key_mismatch", "Idempotency-Key header and body idempotency_key differ")
    key = header if header is not None else body.idempotency_key
    if not key:
        raise AppError(422, "idempotency_key_required", "send Idempotency-Key header or idempotency_key in body")
    if not re.match(IDEMPOTENCY_KEY_PATTERN, key):
        raise AppError(422, "invalid_idempotency_key", "idempotency key must match " + IDEMPOTENCY_KEY_PATTERN)
    return key


@router.post("/shows/{show_id}/reserve", status_code=201)
async def reserve(
    show_id: str, body: ReserveRequest, request: Request, user_id: str = Depends(current_user)
) -> JSONResponse:
    sid = shows.parse_show_id(show_id)
    key = _idempotency_key(request, body)
    settings = request.app.state.settings

    async with request.app.state.pool.acquire(timeout=settings.db_acquire_timeout_s) as conn:
        show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1", sid)
        if show is None:
            raise AppError(404, "show_not_found", "show not found")

        for attempt in range(1, settings.reserve_max_attempts + 1):
            try:
                outcome = await reservations.reserve(conn, show=show, user_id=user_id, req=body, key=key)
                break
            except _TRANSIENT:
                # Shouldn't happen given ordered locking, but a transient
                # conflict must never surface as a 5xx when a retry is safe
                # (the failed txn rolled back completely).
                if attempt == settings.reserve_max_attempts:
                    raise AppError(409, "contention", "too much contention; retry") from None

    if outcome.replayed:
        return JSONResponse(
            status_code=200,
            content=outcome.reservation.model_dump(),
            headers={"Idempotent-Replayed": "true"},
        )
    return JSONResponse(status_code=201, content=outcome.reservation.model_dump())
