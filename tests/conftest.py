import asyncio
import os

import httpx
import pytest

from app.config import Settings
from app.main import create_app

ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
async def app():
    settings = Settings(
        database_url=os.environ.get("TEST_DATABASE_URL", "postgresql://seats:seats@localhost:5432/seats"),
        admin_token="test-admin",
        db_pool_max=30,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(app.state.migrated.wait(), timeout=30)
        yield app


@pytest.fixture
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def admin_headers():
    return dict(ADMIN)


@pytest.fixture
def make_show(client, admin_headers):
    async def _make(seats=None, price_paise=25000, **extra):
        seats = seats or [f"A{i}" for i in range(1, 21)]
        r = await client.post(
            "/shows", json={"name": "t", "seats": seats, "price_paise": price_paise, **extra}, headers=admin_headers
        )
        assert r.status_code == 201, r.text
        return r.json()

    return _make


@pytest.fixture
def auth_for(client):
    cache: dict[str, dict] = {}

    async def _auth(user_id: str) -> dict:
        if user_id not in cache:
            r = await client.post("/auth/token", json={"user_id": user_id})
            assert r.status_code == 200, r.text
            cache[user_id] = {"Authorization": f"Bearer {r.json()['access_token']}"}
        return cache[user_id]

    return _auth


async def assert_reconciled(app, show_id: str) -> dict:
    """Cross-table invariants that must hold after any burst."""
    async with app.state.pool.acquire() as conn:
        counts = await conn.fetchrow(
            """SELECT count(*) AS total,
                      count(*) FILTER (WHERE status = 'available') AS available,
                      count(*) FILTER (WHERE status = 'held') AS held,
                      count(*) FILTER (WHERE status = 'confirmed') AS confirmed
                 FROM seats WHERE show_id = $1""",
            show_id,
        )
        assert counts["available"] + counts["held"] + counts["confirmed"] == counts["total"]
        # Every taken seat belongs to exactly one live reservation of the same user.
        orphans = await conn.fetchval(
            """SELECT count(*) FROM seats s
                 LEFT JOIN reservations r ON r.id = s.reservation_id
                WHERE s.show_id = $1 AND s.status <> 'available'
                  AND (r.id IS NULL OR r.user_id <> s.user_id OR r.status <> s.status
                       OR NOT (s.label = ANY (r.seats)))""",
            show_id,
        )
        assert orphans == 0
        # Live reservations own exactly the seats they list.
        mismatched = await conn.fetchval(
            """SELECT count(*) FROM reservations r
                WHERE r.show_id = $1 AND r.status IN ('held', 'confirmed')
                  AND cardinality(r.seats) <> (SELECT count(*) FROM seats s WHERE s.reservation_id = r.id)""",
            show_id,
        )
        assert mismatched == 0
        # Per-user counters equal the seats each user actually holds.
        drift = await conn.fetchval(
            """SELECT count(*) FROM user_show_seats u
                WHERE u.show_id = $1 AND u.seats_held <> (
                    SELECT count(*) FROM seats s
                     WHERE s.show_id = u.show_id AND s.user_id = u.user_id AND s.status <> 'available')""",
            show_id,
        )
        assert drift == 0
    return dict(counts)
