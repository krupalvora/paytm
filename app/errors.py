"""Uniform error envelope: {"error": {"code": ..., "message": ...}}.

Domain declines (seat taken, over limit, ...) are AppErrors with 4xx codes.
Anything else is a bug and becomes a logged 500.
"""

import asyncio
import logging

import asyncpg
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, status: int, code: str, message: str, **details):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


def _body(code: str, message: str, **details) -> dict:
    err = {"code": code, "message": message}
    if details:
        err.update(details)
    return {"error": err}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError):
        return JSONResponse(status_code=exc.status, content=_body(exc.code, exc.message, **exc.details))

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError):
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content=_body("validation_error", "invalid request", errors=errors))

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException):
        return JSONResponse(status_code=exc.status_code, content=_body("http_error", str(exc.detail)))

    # Dependency failures: honest 503 + Retry-After rather than an opaque 500.
    # Readiness is failing at the same time, so the LB stops sending traffic.
    async def _db_unavailable(_: Request, exc: Exception):
        log.warning("database unavailable: %s: %s", type(exc).__name__, exc)
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "2"},
            content=_body("database_unavailable", "database temporarily unavailable; retry"),
        )

    for exc_type in (
        OSError,  # DNS / connection refused / reset
        asyncpg.PostgresConnectionError,
        asyncpg.CannotConnectNowError,
        asyncpg.InterfaceError,
        asyncpg.TooManyConnectionsError,
        asyncio.TimeoutError,  # pool acquire timed out
    ):
        app.add_exception_handler(exc_type, _db_unavailable)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(status_code=500, content=_body("internal_error", "internal server error"))
