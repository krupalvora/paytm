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
