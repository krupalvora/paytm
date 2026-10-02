import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db
from app.config import Settings, get_settings
from app.errors import install_error_handlers
from app.migrate import migrate_until_done
from app.routes import auth, health, reservations, shows


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        app.state.pool = await db.create_pool(settings)
        # Migrations run in the background so the process comes up (liveness OK)
        # even if the DB is still booting; readiness waits on this event.
        app.state.migrated = asyncio.Event()
        migrator = asyncio.create_task(migrate_until_done(app.state.pool, app.state.migrated))
        try:
            yield
        finally:
            migrator.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await migrator
            await app.state.pool.close()

    app = FastAPI(title="Seat Reservation Service", version="0.1.0", lifespan=lifespan)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(shows.router)
    app.include_router(reservations.router)
    return app


app = create_app()
