from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def liveness() -> dict:
    # Liveness: the process is up and serving. Deliberately does NOT touch the
    # DB, so a DB outage doesn't make the orchestrator restart-loop us.
    return {"status": "ok"}


@router.get("/readyz")
async def readiness(request: Request) -> JSONResponse:
    # Readiness: fail closed if the DB is unreachable, so no traffic is routed
    # to an instance that can't make atomic decisions.
    settings = request.app.state.settings
    try:
        await db.ping(request.app.state.pool, settings.readiness_timeout_s)
    except Exception as exc:  # any failure => not ready
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "checks": {"database": "down"}, "error": type(exc).__name__},
        )
    return JSONResponse(content={"status": "ready", "checks": {"database": "ok"}})
