"""Structured logging, request correlation and Prometheus metrics."""

import contextvars
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram, generate_latest, multiprocess
from prometheus_client.core import GaugeMetricFamily
from starlette.routing import Match

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)

# --------------------------------------------------------------------------- metrics
# Counters are summed across worker processes (multiprocess mode) and reset
# on restart (use rate()/increase()).
# Seat gauges are read from the DB at scrape time, so they always reconcile
# with GET /shows/{id}.

RESERVATIONS_CONFIRMED = Counter(
    "reservations_confirmed_total", "Reservations that became confirmed (direct reserve or hold confirm)"
)
SEATS_CONFIRMED = Counter("seats_confirmed_total", "Seats that became confirmed")
RESERVATIONS_HELD = Counter("reservations_held_total", "Time-boxed holds created")
RESERVATIONS_DECLINED = Counter(
    "reservations_declined_total",
    "Reserve requests that did not create a reservation, by reason",
    ["reason"],
)
RESERVATIONS_RELEASED = Counter(
    "reservations_released_total", "Reservations released back to available", ["reason"]  # cancelled | expired
)
SEATS_RELEASED = Counter("seats_released_total", "Seats released back to available", ["reason"])

# Summed across worker processes in multiprocess mode.
DB_POOL = Gauge("db_pool_connections", "asyncpg pool connections across workers", ["state"],
                multiprocess_mode="livesum")

HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
)

# Map domain error codes on the reserve path to decline reasons.
DECLINE_REASONS = {
    "seat_unavailable": "seat_taken",
    "per_user_limit_exceeded": "per_user_limit",
    "idempotency_key_reused": "idempotency_key_reused",
    "idempotency_conflict": "contention",
    "contention": "contention",
    "unknown_seats": "invalid_request",
    "show_not_found": "invalid_request",
}


class DbStateCollector:
    """Seat gauges computed from Postgres, not from in-process counters.

    The /metrics handler refreshes the snapshot right before rendering, so the
    numbers are exactly what GET /shows/{id} would return -- and they are the
    same no matter which worker process serves the scrape.
    """

    def __init__(self):
        self.snapshot: tuple[dict, dict, dict, bool] = ({}, {}, {}, False)

    async def refresh(self, pool, recent_shows: int, timeout_s: float) -> None:
        try:
            async with pool.acquire(timeout=timeout_s) as conn:
                rows = await conn.fetch(
                    """WITH recent AS (SELECT id, created_at FROM shows ORDER BY created_at DESC LIMIT $1)
                       SELECT s.show_id, extract(epoch FROM r.created_at)::float8 AS created, s.status, count(*) AS n
                         FROM seats s JOIN recent r ON r.id = s.show_id
                        GROUP BY s.show_id, r.created_at, s.status""",
                    recent_shows,
                    timeout=timeout_s,
                )
                violations = await conn.fetchrow(
                    """WITH recent AS (SELECT id FROM shows ORDER BY created_at DESC LIMIT $1)
                       SELECT
                         (SELECT count(*) FROM seats s JOIN recent ON recent.id = s.show_id
                            LEFT JOIN reservations r ON r.id = s.reservation_id
                           WHERE s.status <> 'available'
                             AND (r.id IS NULL OR r.status <> s.status OR r.user_id <> s.user_id))
                           AS seat_without_live_reservation,
                         (SELECT count(*) FROM user_show_seats u JOIN recent ON recent.id = u.show_id
                           WHERE u.seats_held <> (SELECT count(*) FROM seats s
                                                   WHERE s.show_id = u.show_id AND s.user_id = u.user_id
                                                     AND s.status <> 'available'))
                           AS user_counter_drift,
                         (SELECT count(*) FROM reservations r JOIN recent ON recent.id = r.show_id
                           WHERE r.status = 'held' AND r.hold_expires_at < now() - interval '30 seconds')
                           AS holds_overdue_for_expiry""",
                    recent_shows,
                    timeout=timeout_s,
                )
        except Exception:
            logging.getLogger(__name__).warning("metrics db refresh failed", exc_info=True)
            self.snapshot = ({}, {}, {}, False)
            return
        per_show: dict[str, dict[str, int]] = {}
        created: dict[str, float] = {}
        for r in rows:
            sid = str(r["show_id"])
            per_show.setdefault(sid, {"available": 0, "held": 0, "confirmed": 0})[r["status"]] = r["n"]
            created[sid] = r["created"]
        self.snapshot = (per_show, created, dict(violations), True)

    def mark_unavailable(self) -> None:
        self.snapshot = ({}, {}, {}, False)

    def collect(self):
        per_show, created, violations, ok = self.snapshot
        seats = GaugeMetricFamily("seats", "Seats per status for recent shows (read from DB at scrape)",
                                  labels=["show_id", "status"])
        avail = GaugeMetricFamily("seats_available", "Available seats per recent show (read from DB at scrape)",
                                  labels=["show_id"])
        for show_id, c in per_show.items():
            for status, n in c.items():
                seats.add_metric([show_id, status], n)
            seats.add_metric([show_id, "total"], sum(c.values()))
            avail.add_metric([show_id], c["available"])
        born = GaugeMetricFamily("show_created_timestamp_seconds", "Creation time of each tracked show",
                                 labels=["show_id"])
        for show_id, ts in created.items():
            born.add_metric([show_id], ts)
        inv = GaugeMetricFamily("seat_invariant_violations",
                                "Cross-table consistency violations across recent shows (must be 0)", labels=["check"])
        for check, n in violations.items():
            inv.add_metric([check], n)
        scrape = GaugeMetricFamily("seat_metrics_db_scrape_ok", "1 if the DB-backed gauges were refreshed on this scrape",
                                   value=1 if ok else 0)
        return [seats, avail, born, inv, scrape]


DB_STATE = DbStateCollector()
_MULTIPROC = bool(os.environ.get("PROMETHEUS_MULTIPROC_DIR"))
if not _MULTIPROC:
    REGISTRY.register(DB_STATE)


def render_metrics() -> bytes:
    """Process metrics are merged across uvicorn workers when running with
    PROMETHEUS_MULTIPROC_DIR; DB-backed gauges come from this scrape's snapshot."""
    if _MULTIPROC:
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        registry.register(DB_STATE)
        return generate_latest(registry)
    return generate_latest(REGISTRY)


# --------------------------------------------------------------------------- logging

_STD_ATTRS = set(vars(logging.LogRecord("", 0, "", 0, "", None, None))) | {"message", "asctime", "color_message"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k not in _STD_ATTRS and not k.startswith("_") and v is not None:
                out[k] = v
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


_base_factory = logging.getLogRecordFactory()


def _record_factory(*args, **kwargs) -> logging.LogRecord:
    # Stamp correlation ids when the record is CREATED (inside the request's
    # context), not when it is formatted, which may happen elsewhere/later.
    record = _base_factory(*args, **kwargs)
    record.request_id = request_id_var.get()
    record.user_id = user_id_var.get()
    return record


def configure_logging(level: str) -> None:
    logging.setLogRecordFactory(_record_factory)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # Route uvicorn's own loggers through the JSON handler too.
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    # ObservabilityMiddleware writes the access log (with route, ids, latency).
    logging.getLogger("uvicorn.access").disabled = True


# --------------------------------------------------------------------------- middleware

_QUIET_ROUTES = {"/healthz", "/readyz", "/metrics"}
access_log = logging.getLogger("access")


def _route_template(app, scope) -> str:
    # Label by route template, never raw path, to keep metric cardinality bounded.
    for route in app.router.routes:
        match, _ = route.matches(scope)
        if match == Match.FULL:
            return getattr(route, "path", "unmatched")
    return "unmatched"


class ObservabilityMiddleware:
    """Pure ASGI: request id (honours inbound X-Request-ID), access log, RED metrics."""

    def __init__(self, app, fastapi_app):
        self.app = app
        self.fastapi_app = fastapi_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        inbound = headers.get(b"x-request-id", b"").decode("latin-1")
        rid = inbound if 0 < len(inbound) <= 128 else uuid.uuid4().hex
        rid_token = request_id_var.set(rid)
        uid_token = user_id_var.set(None)
        start = time.perf_counter()
        status_holder = {"status": 500}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                status_holder["started"] = True
                message.setdefault("headers", [])
                message["headers"] = list(message["headers"]) + [(b"x-request-id", rid.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            # Handle here (not in Starlette's outer ServerErrorMiddleware) so
            # the stack trace is logged with the request id and the client
            # still gets JSON + X-Request-ID it can quote.
            logging.getLogger("app").exception("unhandled error", extra={"path": scope["path"]})
            if not status_holder.get("started"):
                body = json.dumps({"error": {"code": "internal_error", "message": "internal server error",
                                             "request_id": rid}}).encode()
                await send_wrapper({"type": "http.response.start", "status": 500,
                                    "headers": [(b"content-type", b"application/json")]})
                await send_wrapper({"type": "http.response.body", "body": body})
        finally:
            elapsed = time.perf_counter() - start
            route = _route_template(self.fastapi_app, scope)
            status = status_holder["status"]
            HTTP_REQUESTS.labels(scope["method"], route, str(status)).inc()
            HTTP_LATENCY.labels(scope["method"], route).observe(elapsed)
            pool = getattr(self.fastapi_app.state, "pool", None)
            if pool is not None:
                DB_POOL.labels("size").set(pool.get_size())
                DB_POOL.labels("idle").set(pool.get_idle_size())
                DB_POOL.labels("max").set(pool.get_max_size())
            level = logging.DEBUG if route in _QUIET_ROUTES and status < 500 else logging.INFO
            access_log.log(
                level,
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 2),
                },
            )
            user_id_var.reset(uid_token)
            request_id_var.reset(rid_token)
