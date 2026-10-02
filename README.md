# Seat Reservation Service

Assigned-seat reservation API built for correctness under on-sale stampedes:
no double-sell, per-user limits, idempotent retries.

Stack: Python 3.12 · FastAPI · asyncpg · PostgreSQL 16.

## Run locally

```bash
make up                      # app + postgres via docker compose
curl localhost:8000/healthz  # liveness
curl localhost:8000/readyz   # readiness (checks DB, 503 if down)
```

## Tests

```bash
make db                                   # postgres only
uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

## API

Admin endpoints use `Authorization: Bearer $ADMIN_TOKEN` (default `dev-admin-token` locally).

| Method | Path | Notes |
|---|---|---|
| POST | `/shows` | admin. `{"name", "seats": [...], "price_paise", "per_user_limit"?=4, "hold_ttl_seconds"?=300}` |
| GET | `/shows/{id}` | per-seat status + counts; `available + held + confirmed == total` |
| GET | `/healthz` | liveness |
| GET | `/readyz` | readiness: DB reachable and migrations applied, else 503 |

Money is always integer paise; `250.0` or `"250"` is rejected with 422.
