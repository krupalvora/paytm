"""Reservation decisions.

Where the atomic decision lives: one READ COMMITTED transaction per reserve,
taking locks in a fixed global order so there is no deadlock cycle:

  1. reservations(user_id, idempotency_key)  unique index  -> exactly-once
  2. user_show_seats(show_id, user_id)       guarded upsert -> per-user limit
  3. seats, locked FOR UPDATE in label order, then a conditional UPDATE
     guarded on status = 'available'                       -> no double-sell

Any step that cannot be satisfied raises, the transaction rolls back, and
nothing (seats, counter, idempotency key) moves. Multi-seat requests are
all-or-nothing.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

import asyncpg

from app.errors import AppError
from app.schemas import ReservationView, ReserveRequest


@dataclass(frozen=True)
class ReserveOutcome:
    reservation: ReservationView
    replayed: bool


def request_hash(show_id: uuid.UUID, req: ReserveRequest) -> str:
    # Seat order doesn't change meaning, so ["A2","A1"] == ["A1","A2"].
    canonical = json.dumps({"show_id": str(show_id), "seats": sorted(req.seats), "hold": req.hold}, sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def to_view(row: asyncpg.Record) -> ReservationView:
    return ReservationView(
        reservation_id=str(row["id"]),
        show_id=str(row["show_id"]),
        user_id=row["user_id"],
        seats=list(row["seats"]),
        amount_paise=row["amount_paise"],
        status=row["status"],
        hold_expires_at=row["hold_expires_at"].isoformat() if row["hold_expires_at"] else None,
        created_at=row["created_at"].isoformat(),
    )


class _Replay(Exception):
    """Internal: the idempotency key already exists; resolve outside the txn."""


async def _resolve_existing(conn: asyncpg.Connection, user_id: str, key: str, req_hash: str) -> ReserveOutcome | None:
    row = await conn.fetchrow(
        "SELECT * FROM reservations WHERE user_id = $1 AND idempotency_key = $2", user_id, key
    )
    if row is None:
        return None
    if row["request_hash"] != req_hash:
        raise AppError(
            409, "idempotency_key_reused",
            "idempotency key was already used with a different request",
            reservation_id=str(row["id"]),
        )
    return ReserveOutcome(reservation=to_view(row), replayed=True)


async def _precheck_seats(conn: asyncpg.Connection, show_id: uuid.UUID, seats: list[str]) -> None:
    """Cheap non-locking read that turns away obvious losers early.

    This only ever DECLINES; it never grants. A seat seen as taken was taken at
    the moment of the read, so declining is correct. Winners are still decided
    by the locked conditional UPDATE below. This keeps the 499 losers of a hot
    seat storm from queueing on the winner's row lock.
    """
    rows = await conn.fetch(
        "SELECT label, status FROM seats WHERE show_id = $1 AND label = ANY($2::text[])", show_id, seats
    )
    found = {r["label"]: r["status"] for r in rows}
    unknown = [s for s in seats if s not in found]
    if unknown:
        raise AppError(422, "unknown_seats", "seats do not exist in this show", seats=unknown)
    taken = [s for s in seats if found[s] != "available"]
    if taken:
        raise AppError(409, "seat_unavailable", "seat(s) already taken", seats=taken)


async def reserve(
    conn: asyncpg.Connection,
    *,
    show: asyncpg.Record,
    user_id: str,
    req: ReserveRequest,
    key: str,
) -> ReserveOutcome:
    show_id = show["id"]
    n = len(req.seats)
    req_hash = request_hash(show_id, req)

    # Fast path for retries: a replay must win over "seat taken" (the seat is
    # taken -- by this very reservation).
    existing = await _resolve_existing(conn, user_id, key, req_hash)
    if existing:
        return existing

    if n > show["per_user_limit"]:
        raise AppError(409, "per_user_limit_exceeded", "request exceeds per-user seat limit", limit=show["per_user_limit"])

    try:
        await _precheck_seats(conn, show_id, req.seats)
    except AppError as exc:
        # A concurrent twin with our key may have committed between the key
        # lookup above and this read -- the seat is "taken" by our own
        # reservation. Re-check before declining so retries always replay.
        if exc.code == "seat_unavailable":
            existing = await _resolve_existing(conn, user_id, key, req_hash)
            if existing:
                return existing
        raise

    status = "held" if req.hold else "confirmed"
    try:
        async with conn.transaction():
            # (1) Claim the idempotency key. A concurrent request with the same
            # key blocks here on the unique index until we commit/roll back.
            row = await conn.fetchrow(
                """INSERT INTO reservations
                       (show_id, user_id, seats, amount_paise, status, idempotency_key, request_hash, hold_expires_at)
                   VALUES ($1, $2, $3, $4, $5, $6, $7,
                           CASE WHEN $5 = 'held' THEN now() + $8::interval END)
                   ON CONFLICT (user_id, idempotency_key) DO NOTHING
                   RETURNING *""",
                show_id, user_id, req.seats, show["price_paise"] * n, status, key, req_hash,
                timedelta(seconds=show["hold_ttl_seconds"]),
            )
            if row is None:
                raise _Replay()

            # (2) Per-user limit as a guarded upsert: the row lock serialises a
            # user's parallel requests and the WHERE makes over-limit a no-op.
            held = await conn.fetchval(
                """INSERT INTO user_show_seats (show_id, user_id, seats_held) VALUES ($1, $2, $3)
                   ON CONFLICT (show_id, user_id) DO UPDATE
                       SET seats_held = user_show_seats.seats_held + EXCLUDED.seats_held
                       WHERE user_show_seats.seats_held + EXCLUDED.seats_held <= $4
                   RETURNING seats_held""",
                show_id, user_id, n, show["per_user_limit"],
            )
            if held is None:
                raise AppError(
                    409, "per_user_limit_exceeded", "per-user seat limit reached for this show",
                    limit=show["per_user_limit"],
                )

            # (3) The seat decision. Lock candidate rows in label order (no
            # deadlock between overlapping multi-seat requests), then flip only
            # those still available. Under READ COMMITTED a waiter re-checks
            # status after the lock holder commits, so a loser sees 0 rows.
            won = await conn.fetch(
                """WITH target AS (
                       SELECT label FROM seats
                        WHERE show_id = $1 AND label = ANY($2::text[]) AND status = 'available'
                        ORDER BY label
                        FOR UPDATE)
                   UPDATE seats s
                      SET status = $3, reservation_id = $4, user_id = $5,
                          hold_expires_at = $6, updated_at = now()
                     FROM target
                    WHERE s.show_id = $1 AND s.label = target.label AND s.status = 'available'
                RETURNING s.label""",
                show_id, req.seats, status, row["id"], user_id, row["hold_expires_at"],
            )
            if len(won) != n:
                got = {r["label"] for r in won}
                # All-or-nothing: raising rolls back the seats we did flip.
                raise AppError(409, "seat_unavailable", "seat(s) already taken",
                               seats=[s for s in req.seats if s not in got])
    except _Replay:
        # The other request with our key committed first; replay or reject it.
        existing = await _resolve_existing(conn, user_id, key, req_hash)
        if existing:
            return existing
        # It rolled back between our conflict and our read; the key is free
        # again, so the caller may simply retry.
        raise AppError(409, "idempotency_conflict", "concurrent request with same key; retry") from None

    return ReserveOutcome(reservation=to_view(row), replayed=False)
