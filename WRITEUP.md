# Seat reservation at scale: write-up

## 1. The atomic decision

All of it lives in **one PostgreSQL READ COMMITTED transaction per reserve**
([app/reservations.py](app/reservations.py)). There is no in-memory state, no distributed lock, and no
read-then-write. The transaction runs three guarded writes, always in the same global order:

| # | Statement | What it guarantees |
|---|---|---|
| 1 | `INSERT INTO reservations … ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING *` | exactly-once per key |
| 2 | `INSERT INTO user_show_seats … ON CONFLICT DO UPDATE SET seats_held = seats_held + n WHERE seats_held + n <= limit RETURNING seats_held` | per-user limit |
| 3 | `WITH target AS (SELECT … WHERE status='available' ORDER BY label FOR UPDATE) UPDATE seats … WHERE status='available' RETURNING label` | no double-sell |

If statement 2 or 3 returns fewer rows than needed, the code raises and the transaction rolls back.
Nothing moves: no seat, no counter, no idempotency key.

**Why it's race-free.** The seat row is the unit of contention. Five hundred transactions racing
for A12 all block on that row's lock. The first commits `status='confirmed'`. Each waiter then
wakes up and Postgres re-evaluates `status='available'` against the *new* row version (EvalPlanQual
recheck under READ COMMITTED). The predicate is now false, the UPDATE returns 0 rows, and that
request becomes a clean 409. Double-selling would require two transactions to both see
`available` *and* both write. The row lock makes the second one wait, and the recheck makes it
fail. A `CHECK` constraint on `seats` also makes a half-written seat impossible: status and
owner fields must agree.

**Per-user limit.** The counter row for (show, user) is locked by the upsert. That queues one
user's parallel requests on the same show one after another, and the `WHERE` makes going over the
limit a no-op. Ten parallel requests against a limit of 4 give exactly 4 successes, and the tests
check this with the cheap pre-read disabled. The counter is decremented in the same transaction
as every release. The test suite and the `/metrics` invariant gauge both check that each user's
counter equals the number of seats they actually hold.

**Multi-seat requests and deadlock.** Requests are all-or-nothing. Candidate seats are locked
`ORDER BY label`, so two requests for `[A1,A2]` and `[A2,A1]` both lock A1 first, and a lock cycle
can't form. Across tables the order is always reservation, then counter, then seats (by label),
including cancel, confirm and the expiry job. A test fires 120 overlapping multi-seat requests in
reversed orders and gets zero deadlocks. Deadlock and serialization errors are still caught and
retried up to 3 times as a safety net. If retries ran out, the result would be a 409
`contention` (alerted on), never a 5xx.

**The cheap pre-read.** Before the transaction, one plain `SELECT` turns away requests whose seat
is already taken. It can **only decline, never grant**: a seat that read as taken really was taken
at that moment. Without it, the 499 losers of a hot-seat storm would queue on the winner's row
lock and hold database connections while they wait. One subtle race was caught by the tests. A
retry could look up its key, then the original request commits, and then the retry's pre-read
sees the seat "taken", by its own reservation. So before declining for "seat taken", the key is
looked up once more.

## 2. Idempotency

- **Storage:** the `reservations` row itself, with `UNIQUE (user_id, idempotency_key)` and a
  `request_hash` (sha256 of show id, sorted seats, and the hold flag). Keys are scoped per user, so
  two users can't collide on a key and one user can't replay another user's key.
- **Exactly-once:** the unique index enforces it, not application code. A concurrent duplicate
  blocks on the index entry until the first transaction finishes. If the first commits, the
  duplicate's `ON CONFLICT DO NOTHING` returns nothing, its transaction rolls back, and it reads
  and returns the committed row. If the first rolls back, the duplicate simply proceeds.
- **Replay:** returns **200** with the original reservation (in its current status) and
  `Idempotent-Replayed: true`. I chose 200 rather than 201 so that "exactly one 201 per hot seat"
  stays strictly true even when the testers send retries.
- **Same key, different body:** the hash differs, so the response is **409
  `idempotency_key_reused`** and nothing moves. Seat order isn't part of the identity
  (`[A2,A1]` == `[A1,A2]`).
- **Declined attempts don't consume the key.** The transaction rolled back, so a retry of a
  declined request is evaluated fresh. That's deliberate: a 409 is a fact about the seat at that
  moment, not a cached response.

## 3. Holds and expiry

Both release models are implemented:

- `hold: false` (the default) confirms immediately. `hold: true` creates a `held` reservation
  with `hold_expires_at = now() + hold_ttl_seconds`, and `POST /reservations/{id}/confirm` turns it
  into `confirmed`.
- `POST /reservations/{id}/cancel` is owner-only, safe to repeat, and works on held or confirmed
  reservations. Anyone else gets 404, which doesn't reveal that the reservation exists.
- **Expiry job:** every app process runs one. It expires one hold per transaction using
  `SELECT … FOR UPDATE SKIP LOCKED`, so several instances share the work without blocking each
  other or blocking a user's cancel or confirm. A hold can show as `held` for at most about 1s past
  expiry, and `confirm` refuses an expired hold regardless. It releases the seats on the spot and
  returns 409 `hold_expired`.
- **No resurrection:** every release updates seats `WHERE reservation_id = <this reservation>`.
  A stale cancel or expiry of reservation R can only touch seats R still owns. It can never free a
  seat that has since been sold to someone else. If R owns fewer seats than it lists, the release
  rolls back loudly instead of letting the counter drift.

## 4. Consistency vs availability under a partition

This is a **CP** system by choice: correctness of who owns a seat comes before answering.

- Every decision goes to the single Postgres primary. If the app can't reach it, reserves return
  **503 `database_unavailable`** with `Retry-After`, `/readyz` fails so the load balancer stops
  routing traffic, and `/healthz` stays green so the orchestrator doesn't restart-loop a healthy
  process. No instance ever "optimistically" grants a seat from a cache.
- Retrying is safe because of idempotency. A client that timed out mid-partition retries with the
  same key and gets exactly one outcome.
- What could be relaxed safely: the **read** path. `GET /shows/{id}` could come from a replica or
  a cache and be slightly stale. A stale "available" only costs the user a 409 on reserve, because
  the decision still happens on the primary.
- Scaling past one primary means sharding by `show_id`. Every contended row (seats, counters,
  keys) is scoped to one show, so a show's whole decision stays on one shard and there are no
  cross-shard transactions.

## 5. Observability: what pages at 2am

Metrics are at `/metrics`, the dashboard at `/grafana/`, the rules in
[deploy/prometheus/alerts.yml](deploy/prometheus/alerts.yml), and live logs at `/logs/`.
Logs are JSON lines carrying `request_id` (accepted from an incoming `X-Request-ID` and echoed
back) and `user_id`.

| Alert | Why it pages |
|---|---|
| `SeatInvariantViolated`: `sum(seat_invariant_violations) > 0` | Computed from the DB on every scrape: a taken seat without a matching live reservation, or a user counter that disagrees with seats held. This is the "we may have double-sold" alarm. |
| `Reservation5xx` | Every decline is a 4xx by design, so any 5xx is a bug or an infrastructure failure. |
| `HoldsNotExpiring` | Holds more than 30s past expiry: the expiry job is stuck and inventory is silently locked away. |
| `ServiceDown` / `DatabaseUnreachableFromMetrics` | The service or the database is unreachable. |
| `ReserveLatencyHigh` (p99 > 2s), `DbPoolSaturated`, `ReserveContentionDeclines` | Tickets, not pages: capacity or lock-ordering regressions. |

Seat gauges (`seats{show_id,status}`, `seats_available`) are **read from Postgres at scrape time**,
not kept in memory. So they always match `GET /shows/{id}` and survive restarts. Counters
(`reservations_confirmed_total`, `reservations_declined_total{reason}`) use Prometheus
multiprocess mode, which sums them across uvicorn workers. The burst script compares them with
what the client saw and they match exactly.

## 6. Deployment and load behaviour

The target is one VM with everything in Docker Compose: Caddy, the app, Postgres, Prometheus,
Grafana and Dozzle. `deploy/bootstrap.sh` sets it up with one command and automatic HTTPS
through sslip.io.

The key load decision is that **Caddy is the queue**. `max_conns_per_host` caps concurrent
requests to the app (256 by default). Requests beyond that wait inside Caddy, where they are cheap
goroutines, rather than piling up as Python coroutines and database pool waiters. When overloaded,
the service gets slower; it doesn't return 5xx or run out of memory. Measured locally with 4
workers through Caddy:

- about 21.6k requests in 53s;
- every burst check passed, with zero 5xx;
- latency p50 0.8s, p99 5.9s at 500 concurrent requests.

## 7. AI usage

> **Draft: rewrite this section in your own words before submitting.** It has to describe what
> *you* did. It lists only what is visible from this session; add your own review, changes and
> reasoning.

I built this with Claude Code (Opus) as a pair, one step and one commit at a time. I reviewed and
committed each step myself.

**What I decided:**
- the stack (Python + FastAPI + Postgres);
- supporting both release models (an explicit cancel and expiring holds);
- deploying to a single VM with Postgres on the same machine instead of a PaaS;
- the incremental, reviewable commit structure.

*[Add: anything you changed, rejected or questioned.]*

**What I directed and the AI implemented:**
- the 7-step plan;
- the schema;
- the transaction design and lock ordering;
- the tests;
- the burst script;
- the deploy stack;
- the first draft of this write-up.

**Where the tests caught AI mistakes:**
- a retry could be declined as "seat taken" by its own reservation (the pre-read race in §1);
- a detail field named `status` clashed with the error class's own argument, which would have
  turned a 409 into a 500;
- the "hold expired" error was raised inside the transaction, which would have rolled back the
  release it reported;
- the request id went missing from log lines formatted after the request finished;
- a `docker compose stop` in a test script silently didn't run because of zsh word-splitting, so
  an outage test "passed" without testing anything. Caught by sanity-checking a suspicious result.

*[Add: the parts you can explain and extend live without help: e.g. walk through the EvalPlanQual
recheck, the lock order, why replay is 200.]*

## 8. What I'd do next

- **Payment step:** hold → payment intent → confirm, with the payment provider's idempotency key
  stored alongside ours. Use an outbox table so "confirmed" and "charge captured" can't diverge.
- **Faster losers:** cache seats already confirmed for the hot show in memory, invalidated through
  `LISTEN/NOTIFY`. That turns most hot-seat declines into zero database round trips. It's only
  safe because confirmed seats change state rarely, and it only ever declines.
- **Fairness at on-sale:** a virtual waiting room (token bucket per show) in front of reserve, so
  the stampede is admitted at a rate the database can sustain, rather than at random by whoever's
  TCP connection lands first.
- **Key retention:** expire idempotency keys after N days, using a partitioned table or a TTL job.
- **Hardening:**
  - put the Docker socket behind a read-only proxy for Dozzle;
  - wire Alertmanager to a pager;
  - add Postgres backups (WAL archiving);
  - add a read replica for `GET /shows/{id}`;
  - add per-IP rate limiting at Caddy.
- **Multi-instance:** several app VMs behind a load balancer sharing one Postgres. The design
  already supports it: no state in the process, and the expiry job uses `SKIP LOCKED`.
