# Seat Reservation Service

Assigned-seat reservation API built for correctness under on-sale stampedes:
no double-sell, per-user limits, idempotent retries.

Stack: Python 3.12 · FastAPI · asyncpg · PostgreSQL 16 · Caddy · Prometheus · Grafana.
Design rationale: [WRITEUP.md](WRITEUP.md).

## Live deployment

| | URL |
|---|---|
| API | https://13-126-126-106.sslip.io |
| Health | https://13-126-126-106.sslip.io/healthz, https://13-126-126-106.sslip.io/readyz |
| Metrics | https://13-126-126-106.sslip.io/metrics |
| Dashboard | https://13-126-126-106.sslip.io/grafana/ (anonymous, read-only) |
| Alert rules | https://13-126-126-106.sslip.io/prometheus/alerts |
| Live logs | https://13-126-126-106.sslip.io/logs/ (basic auth, credentials shared separately) |

Hosted on a single AWS EC2 VM (ap-south-1) via [docker-compose.prod.yml](docker-compose.prod.yml).
The admin token is shared separately; user tokens come from `POST /auth/token`.

```bash
./burst.sh https://13-126-126-106.sslip.io --admin-token <ADMIN_TOKEN>
```

## Try it

```bash
BASE=http://localhost:8000; ADMIN=dev-admin-token
SHOW=$(curl -s -X POST $BASE/shows -H "Authorization: Bearer $ADMIN" -H 'content-type: application/json' \
  -d '{"name":"friday-night","seats":["A1","A2","A3","A12","A13"],"price_paise":25000}' | jq -r .id)
TOKEN=$(curl -s -X POST $BASE/auth/token -H 'content-type: application/json' -d '{"user_id":"alice"}' | jq -r .access_token)
curl -s -X POST $BASE/shows/$SHOW/reserve -H "Authorization: Bearer $TOKEN" -H 'Idempotency-Key: k1' \
  -H 'content-type: application/json' -d '{"seats":["A12"]}'
curl -s $BASE/shows/$SHOW | jq .counts
```

## Run locally

```bash
make up                      # app + postgres via docker compose
curl localhost:8000/healthz  # liveness
curl localhost:8000/readyz   # readiness (checks DB, 503 if down)
```

## Deploy (single VM: DigitalOcean droplet / AWS EC2)

Everything runs from [docker-compose.prod.yml](docker-compose.prod.yml) on one Ubuntu VM:

```
internet ─▶ Caddy :443 (auto-TLS, admission control: ≤256 concurrent upstream conns)
              ├─▶ app  (uvicorn × WEB_CONCURRENCY workers) ─▶ Postgres 16 (volume)
              ├─▶ /grafana/  ─▶ Grafana ─▶ Prometheus (scrapes app /metrics every 5s, alert rules)
              └─▶ /logs/     ─▶ Dozzle (live docker logs, basic auth)
```

1. Create a VM: Ubuntu 22.04/24.04, **2+ vCPU / 2+ GB RAM** recommended (the burst is CPU-bound).
   Open inbound **22, 80, 443** in the security group or cloud firewall.
2. On the VM:
   ```bash
   git clone <this repo> seats && cd seats
   sudo ./deploy/bootstrap.sh                    # HTTPS at https://<ip-with-dashes>.sslip.io, no domain needed
   # or: sudo ./deploy/bootstrap.sh seats.example.com   (A record -> VM IP)
   # or: sudo ./deploy/bootstrap.sh :80                 (plain HTTP on the IP)
   ```
   It installs Docker, tunes kernel network limits, generates `.env` with random secrets
   (admin token, JWT secret, DB/Grafana/log passwords), starts the stack, waits for `/readyz`
   and prints every URL and credential. Re-running is safe; secrets are kept.
3. Later deploys: `git pull && docker compose -f docker-compose.prod.yml up -d --build`.

**Cold start / reboot:** every service has `restart: unless-stopped`. If the app comes up
before Postgres, it serves `/healthz` and keeps `/readyz` at 503 while migrations retry. It
turns ready about 2s after the DB is reachable. While the app restarts, Caddy holds requests
for up to 30s (`lb_try_duration`) instead of returning 502.

**Tuning knobs** (`.env`): `WEB_CONCURRENCY` (workers; default = vCPUs, max 4), `DB_POOL_MAX`
(per worker; workers × pool must stay under Postgres `max_connections`=200), `UPSTREAM_MAX_CONNS`.

## Tests

```bash
make db                                   # postgres only
uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

## Burst test (one command)

```bash
./burst.sh <BASE_URL> --admin-token <ADMIN_TOKEN>     # or: make burst URL=<BASE_URL> ADMIN_TOKEN=<...>
./burst.sh http://localhost:8000 --quick              # small local smoke run
```

Needs `uv` (deps come from the script header), or Docker, or `python3` with `httpx`.
It creates a fresh show (2000 seats, limit 4), mints user tokens, then fires ~20,000
reserve requests at once (concurrency 500), mixing:

- **hot-seat**: 500 distinct users storm each of 5 seats, so expect exactly one 201 per seat
- **limit**: 50 users each fire 10 parallel single-seat reserves, so at most 4 each
- **idempotent**: 100 users each send one key 20× in parallel, so one reservation per key
- **hold**: 100 users hold a seat, then half confirm and half cancel
- **general**: random users and seats, 10% sent twice with the same key

While it runs, it polls `GET /shows/{id}` to check `available + held + confirmed == total`. Then it
probes same-key-different-body (expects 409) and identity spoofing (body `user_id` must be ignored,
another user's cancel must get 404), and prints:

- HTTP status, outcome and per-scenario distributions (confirmed / declined by reason / 5xx), latency percentiles
- `/metrics` deltas next to what the client observed
- PASS/FAIL for each correctness rule, including the final seat map exactly matching the set of
  successful responses and the metrics gauges matching the API

Exit code is non-zero if any check fails; a JSON summary goes to `burst-results/`.
Tune with `--requests`, `--concurrency`, `--hot-users`, ... (`./burst.sh x --help`).

## API

Admin endpoints use `Authorization: Bearer $ADMIN_TOKEN` (default `dev-admin-token` locally).

| Method | Path | Notes |
|---|---|---|
| POST | `/shows` | admin. `{"name", "seats": [...], "price_paise", "per_user_limit"?=4, "hold_ttl_seconds"?=300}` |
| POST | `/auth/token` | demo IdP: `{"user_id"}` -> HS256 JWT (disable with `ALLOW_TOKEN_ISSUE=false`) |
| POST | `/shows/{id}/reserve` | user token. `{"seats": [...], "idempotency_key"?, "hold"?=false}` or `Idempotency-Key` header. 201 new, 200 + `Idempotent-Replayed: true` on retry |
| GET | `/reservations/{id}` | owner only (others get 404) |
| POST | `/reservations/{id}/confirm` | owner only; held -> confirmed while unexpired, else 409 `hold_expired` / `reservation_not_active`; idempotent |
| POST | `/reservations/{id}/cancel` | owner only; releases held or confirmed seats; idempotent |
| GET | `/shows/{id}` | per-seat status + counts; `available + held + confirmed == total` |
| GET | `/metrics` | Prometheus exposition |
| GET | `/healthz` | liveness |
| GET | `/readyz` | readiness: DB reachable and migrations applied, else 503 |

User identity comes only from the token's `sub`; a `user_id` in a request body is ignored.

### Reserve outcomes

| Status | `error.code` | Meaning |
|---|---|---|
| 201 | – | reserved (all requested seats, `status: confirmed`, or `held` with `"hold": true`) |
| 200 | – | idempotent replay: same user + key + request; returns the original reservation |
| 409 | `seat_unavailable` | one or more seats taken; **all-or-nothing**, nothing was reserved |
| 409 | `per_user_limit_exceeded` | would exceed the show's `per_user_limit` (held + confirmed) |
| 409 | `idempotency_key_reused` | same key, different request (seats/show/hold) |
| 422 | `unknown_seats`, `validation_error`, `idempotency_key_required` | bad request |
| 503 | `database_unavailable` | Postgres unreachable (with `Retry-After`); `/readyz` is failing at the same time |

Every domain decline is a 4xx. A 5xx means the infrastructure failed or there is a bug, and it pages.

### Holds and release

- `"hold": false` (default) books seats as `confirmed` immediately.
- `"hold": true` creates a `held` reservation that expires after the show's `hold_ttl_seconds`
  (default 300) unless confirmed. A background reaper in every instance (`FOR UPDATE SKIP LOCKED`,
  every `REAPER_INTERVAL_S`=1s) returns expired seats to `available`; `confirm` refuses an expired
  hold even if the reaper hasn't run yet.
- Cancel / expiry only release seats whose `reservation_id` is still that reservation, so a
  release can never free a seat that has since been sold to someone else.
- Cancel/expiry free the user's per-show limit. A replayed idempotency key returns the reservation
  in its current state (e.g. `cancelled`); it never re-reserves.

Money is always integer paise; `250.0` or `"250"` is rejected with 422.

## Observability

**Logs**: one JSON object per line on stdout. Every line logged during a request carries
`request_id` (taken from inbound `X-Request-ID`, else generated, and echoed back in the
response header) and `user_id` once authenticated. Each request ends with an `access` line
(`method`, `route` template, `status`, `duration_ms`); reserve outcomes log
`reserve succeeded` / `reserve declined` (with `reason`) / `reserve replayed`.

**Metrics** (`GET /metrics`):

| Metric | Type | Meaning |
|---|---|---|
| `reservations_confirmed_total` | counter | reservations that became confirmed (direct or via hold confirm) |
| `seats_confirmed_total` | counter | seats that became confirmed |
| `reservations_held_total` | counter | holds created |
| `reservations_declined_total{reason}` | counter | `seat_taken`, `per_user_limit`, `idempotent_replay`, `idempotency_key_reused`, `contention`, `invalid_request` |
| `reservations_released_total{reason}` / `seats_released_total{reason}` | counter | `cancelled`, `expired` |
| `seats{show_id,status}` / `seats_available{show_id}` | gauge | read from Postgres at scrape time for the 20 most recent shows, so it always matches `GET /shows/{id}` |
| `seat_invariant_violations{check}` | gauge | cross-table checks; anything non-zero is a page |
| `seat_metrics_db_scrape_ok` | gauge | 0 if the DB-backed gauges could not be refreshed |
| `http_requests_total{method,route,status}`, `http_request_duration_seconds` | counter/histogram | RED metrics by route template |
| `db_pool_connections{state}` | gauge | asyncpg pool size / idle / max |

Counters are per process and reset on restart; read them with `increase()`/`rate()`.
