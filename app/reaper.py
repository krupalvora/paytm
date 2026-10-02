"""Background expiry of time-boxed holds.

Every instance runs one; FOR UPDATE SKIP LOCKED in expire_one() means they
cooperate instead of fighting. A hold is therefore visible as "held" for at
most ~reaper_interval_s past its expiry, and confirm() refuses expired holds
regardless of whether the reaper has run.
"""

import asyncio
import logging

import asyncpg

from app.reservations import expire_one

log = logging.getLogger(__name__)


async def run_reaper(pool: asyncpg.Pool, migrated: asyncio.Event, interval_s: float, batch: int) -> None:
    await migrated.wait()
    while True:
        try:
            expired = 0
            async with pool.acquire() as conn:
                while expired < batch and await expire_one(conn):
                    expired += 1
            if expired:
                log.info("expired holds", extra={"count": expired})
            if expired >= batch:
                continue  # backlog: go again without sleeping
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reaper iteration failed")
        await asyncio.sleep(interval_s)
