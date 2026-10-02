import asyncio

import asyncpg

from app.config import Settings


def _normalize_dsn(dsn: str) -> str:
    # Render/Heroku hand out postgres:// URLs; asyncpg accepts both, but be explicit.
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://"):]
    return dsn


async def create_pool(settings: Settings) -> asyncpg.Pool:
    # min_size=0 means the pool can be created even while the DB is still
    # booting (cold start); readiness reports the truth until it comes up.
    return await asyncpg.create_pool(
        dsn=_normalize_dsn(settings.database_url),
        min_size=0,
        max_size=settings.db_pool_max,
        command_timeout=30,
    )


async def ping(pool: asyncpg.Pool, timeout_s: float) -> None:
    """Raise if the database is not reachable within timeout_s."""
    async def _q() -> None:
        async with pool.acquire(timeout=timeout_s) as conn:
            await conn.fetchval("SELECT 1")

    await asyncio.wait_for(_q(), timeout=timeout_s)
