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
