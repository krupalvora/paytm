import uuid

import asyncpg

from app.errors import AppError
from app.schemas import CreateShowRequest, SeatCounts, SeatView, ShowView


def parse_show_id(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError(404, "show_not_found", "show not found") from None


async def create_show(
    conn: asyncpg.Connection, req: CreateShowRequest, per_user_limit: int, hold_ttl_seconds: int
) -> uuid.UUID:
    async with conn.transaction():
        show_id = await conn.fetchval(
            """INSERT INTO shows (name, price_paise, per_user_limit, hold_ttl_seconds, total_seats)
               VALUES ($1, $2, $3, $4, $5) RETURNING id""",
            req.name, req.price_paise, per_user_limit, hold_ttl_seconds, len(req.seats),
        )
        # Single set-based insert; every seat starts available.
        await conn.execute(
            """INSERT INTO seats (show_id, label, position)
               SELECT $1, s.label, s.ord FROM unnest($2::text[]) WITH ORDINALITY AS s(label, ord)""",
            show_id, req.seats,
        )
    return show_id


async def get_show(conn: asyncpg.Connection, show_id: uuid.UUID) -> ShowView:
    show = await conn.fetchrow("SELECT * FROM shows WHERE id = $1", show_id)
    if show is None:
        raise AppError(404, "show_not_found", "show not found")

    # Counts are derived from the same single SELECT as the seat list, so they
    # come from one MVCC snapshot and available + held + confirmed == total
    # holds exactly in every response, even mid-burst.
    rows = await conn.fetch(
        "SELECT label, status FROM seats WHERE show_id = $1 ORDER BY position", show_id
    )
    counts = {"available": 0, "held": 0, "confirmed": 0}
    for r in rows:
        counts[r["status"]] += 1

    return ShowView(
        id=str(show["id"]),
        name=show["name"],
        price_paise=show["price_paise"],
        per_user_limit=show["per_user_limit"],
        hold_ttl_seconds=show["hold_ttl_seconds"],
        total_seats=show["total_seats"],
        created_at=show["created_at"].isoformat(),
        counts=SeatCounts(total=len(rows), **counts),
        seats=[SeatView(seat=r["label"], status=r["status"]) for r in rows],
    )
