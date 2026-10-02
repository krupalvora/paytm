"""Tiny forward-only migration runner.

Applies migrations/NNN_*.sql in order, each in its own transaction, recording
applied versions in schema_migrations. A session-level advisory lock makes it
safe when several instances boot at once: one migrates, the rest wait and then
see nothing left to do.
"""

import asyncio
import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_LOCK_KEY = 0x5EA7_0001  # arbitrary, constant across instances


async def apply_migrations(pool: asyncpg.Pool) -> list[str]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied_now: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        try:
            await conn.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                       version    text PRIMARY KEY,
                       applied_at timestamptz NOT NULL DEFAULT now())"""
            )
            done = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for f in files:
                version = f.stem
                if version in done:
                    continue
                async with conn.transaction():
                    await conn.execute(f.read_text())
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                applied_now.append(version)
                log.info("migration applied", extra={"version": version})
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    return applied_now


async def migrate_until_done(pool: asyncpg.Pool, done: asyncio.Event, retry_s: float = 2.0) -> None:
    """Retry forever: on a cold start the DB may come up after we do.

    Readiness stays 503 until `done` is set, so no traffic arrives early.
    """
    while True:
        try:
            await apply_migrations(pool)
            done.set()
            return
        except Exception as exc:
            log.warning("migrations not applied yet, retrying: %s: %s", type(exc).__name__, exc)
            await asyncio.sleep(retry_s)
