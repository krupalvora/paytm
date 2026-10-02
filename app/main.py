from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.config import Settings, get_settings
from app.routes import health


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.pool = await db.create_pool(settings)
        try:
            yield
        finally:
            await app.state.pool.close()

    app = FastAPI(title="Seat Reservation Service", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    return app


app = create_app()
