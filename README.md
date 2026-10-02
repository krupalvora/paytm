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
| POST | `/auth/token` | demo IdP: `{"user_id"}` -> HS256 JWT (disable with `ALLOW_TOKEN_ISSUE=false`) |
| POST | `/shows/{id}/reserve` | user token. `{"seats": [...], "idempotency_key"?, "hold"?=false}` or `Idempotency-Key` header. 201 new, 200 + `Idempotent-Replayed: true` on retry |
| GET | `/reservations/{id}` | owner only (others get 404) |
| POST | `/reservations/{id}/confirm` | owner only; held -> confirmed while unexpired, else 409 `hold_expired` / `reservation_not_active`; idempotent |
| POST | `/reservations/{id}/cancel` | owner only; releases held or confirmed seats; idempotent |
| GET | `/shows/{id}` | per-seat status + counts; `available + held + confirmed == total` |
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
