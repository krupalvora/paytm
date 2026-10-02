import logging
import re

import asyncpg
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app import observability as obs
from app import reservations, shows
from app.auth import current_user
from app.errors import AppError
from app.schemas import IDEMPOTENCY_KEY_PATTERN, ReserveRequest

router = APIRouter(tags=["reservations"])
log = logging.getLogger("reservations")

_TRANSIENT = (asyncpg.DeadlockDetectedError, asyncpg.SerializationError)


def _conn(request: Request):
    return request.app.state.pool.acquire(timeout=request.app.state.settings.db_acquire_timeout_s)


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
    try:
        key = _idempotency_key(request, body)
        outcome = await _reserve(request, sid, body, user_id, key)
    except AppError as exc:
        reason = obs.DECLINE_REASONS.get(exc.code, "invalid_request")
        obs.RESERVATIONS_DECLINED.labels(reason).inc()
        log.info("reserve declined", extra={"show_id": show_id, "seats": body.seats, "reason": reason,
                                            "code": exc.code})
        raise

    r = outcome.reservation
    if outcome.replayed:
        obs.RESERVATIONS_DECLINED.labels("idempotent_replay").inc()
        log.info("reserve replayed", extra={"show_id": show_id, "reservation_id": r.reservation_id,
                                            "reason": "idempotent_replay"})
        return JSONResponse(status_code=200, content=r.model_dump(), headers={"Idempotent-Replayed": "true"})

    if r.status == "confirmed":
        obs.RESERVATIONS_CONFIRMED.inc()
        obs.SEATS_CONFIRMED.inc(len(r.seats))
    else:
        obs.RESERVATIONS_HELD.inc()
    log.info("reserve succeeded", extra={"show_id": show_id, "reservation_id": r.reservation_id,
                                         "seats": r.seats, "status": r.status, "amount_paise": r.amount_paise})
    return JSONResponse(status_code=201, content=r.model_dump())


async def _reserve(request: Request, sid, body: ReserveRequest, user_id: str, key: str) -> reservations.ReserveOutcome:
    settings = request.app.state.settings
    async with _conn(request) as conn:
        show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1", sid)
        if show is None:
            raise AppError(404, "show_not_found", "show not found")

        for attempt in range(1, settings.reserve_max_attempts + 1):
            try:
                return await reservations.reserve(conn, show=show, user_id=user_id, req=body, key=key)
            except _TRANSIENT as exc:
                # Shouldn't happen given ordered locking, but a transient
                # conflict must never surface as a 5xx when a retry is safe
                # (the failed txn rolled back completely).
                log.warning("transient db conflict on reserve", extra={"attempt": attempt,
                                                                       "error": type(exc).__name__})
                if attempt == settings.reserve_max_attempts:
                    raise AppError(409, "contention", "too much contention; retry") from None
    raise AssertionError("unreachable")


@router.get("/reservations/{reservation_id}")
async def get_reservation(reservation_id: str, request: Request, user_id: str = Depends(current_user)) -> dict:
    rid = reservations.parse_reservation_id(reservation_id)
    async with _conn(request) as conn:
        return (await reservations.get_reservation(conn, rid, user_id)).model_dump()


@router.post("/reservations/{reservation_id}/cancel")
async def cancel(reservation_id: str, request: Request, user_id: str = Depends(current_user)) -> dict:
    """Owner-only; idempotent. Releases held or confirmed seats back to available."""
    rid = reservations.parse_reservation_id(reservation_id)
    async with _conn(request) as conn:
        view, released = await reservations.cancel(conn, rid, user_id)
    if released:
        obs.RESERVATIONS_RELEASED.labels("cancelled").inc()
        obs.SEATS_RELEASED.labels("cancelled").inc(len(view.seats))
        log.info("reservation cancelled", extra={"reservation_id": view.reservation_id, "seats": view.seats})
    return view.model_dump()


@router.post("/reservations/{reservation_id}/confirm")
async def confirm(reservation_id: str, request: Request, user_id: str = Depends(current_user)) -> dict:
    """Owner-only; idempotent. Turns an unexpired hold into a confirmed booking."""
    rid = reservations.parse_reservation_id(reservation_id)
    async with _conn(request) as conn:
        view, confirmed = await reservations.confirm(conn, rid, user_id)
    if confirmed:
        obs.RESERVATIONS_CONFIRMED.inc()
        obs.SEATS_CONFIRMED.inc(len(view.seats))
        log.info("hold confirmed", extra={"reservation_id": view.reservation_id, "seats": view.seats})
    return view.model_dump()
