# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27"]
# ///
"""On-sale stampede against a live seat-reservation service.

Creates a fresh show, then fires every scenario at once:

  hot-seat   many distinct users storm the same few seats   -> exactly one 201 per seat
  limit      users fire 10 parallel single-seat reserves     -> at most per_user_limit each
  idempotent users retry the same key many times in parallel -> one reservation per key
  hold       users hold seats, then confirm or cancel        -> exact final seat states
  general    random users / seats with some same-key retries -> zero 5xx

and polls GET /shows/{id} throughout to check available + held + confirmed == total.
Afterwards: idempotency-key reuse with a different body, identity spoofing, final
reconciliation (API seat map vs. what the 201s say vs. /metrics gauges).

Exit code 0 only if every check passes.

    uv run burst/burst.py https://your-app.onrender.com --admin-token $ADMIN_TOKEN
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import httpx

# ----------------------------------------------------------------------------- plumbing


@dataclass
class Result:
    scenario: str
    user: str
    status: int  # 0 = transport failure after retries
    code: str | None
    latency_s: float
    body: dict = field(default_factory=dict)
    key: str | None = None

    @property
    def reservation_id(self) -> str | None:
        return self.body.get("reservation_id")


class Api:
    def __init__(self, base: str, concurrency: int, timeout_s: float, transport_retries: int):
        self.base = base.rstrip("/")
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(
            base_url=self.base,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 15)),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            http2=False,
        )
        self.transport_retries = transport_retries
        self.transport_errors = 0

    async def close(self):
        await self.client.aclose()

    async def call(self, method: str, path: str, *, headers=None, json_body=None) -> tuple[int, dict, float]:
        """Retries only transport failures (timeouts, resets). Safe for reserve
        because the retry reuses the same idempotency key, exactly like a real
        client would."""
        last_exc = None
        for _ in range(self.transport_retries + 1):
            async with self.sem:
                t0 = time.perf_counter()
                try:
                    r = await self.client.request(method, path, headers=headers, json=json_body)
                except httpx.TransportError as exc:
                    self.transport_errors += 1
                    last_exc = exc
                    continue
                latency = time.perf_counter() - t0
            try:
                body = r.json()
            except ValueError:
                body = {"_raw": r.text[:200]}
            return r.status_code, body, latency
        return 0, {"_transport_error": type(last_exc).__name__}, 0.0


def err_code(body: dict) -> str | None:
    e = body.get("error")
    return e.get("code") if isinstance(e, dict) else None


# ----------------------------------------------------------------------------- setup


async def wait_ready(api: Api, budget_s: float) -> None:
    deadline = time.monotonic() + budget_s
    while True:
        try:
            r = await api.client.get("/readyz", timeout=10)
            if r.status_code == 200:
                return
            note = r.text[:120]
        except httpx.HTTPError as exc:
            note = type(exc).__name__
        if time.monotonic() > deadline:
            sys.exit(f"service not ready after {budget_s:.0f}s: {note}")
        print(f"  waiting for /readyz ({note})", flush=True)
        await asyncio.sleep(3)


async def mint_tokens(api: Api, users: list[str]) -> dict[str, dict]:
    async def one(u: str):
        status, body, _ = await api.call("POST", "/auth/token", json_body={"user_id": u})
        if status != 200:
            sys.exit(f"could not mint token for {u}: {status} {body}")
        return u, {"Authorization": f"Bearer {body['access_token']}"}

    return dict(await asyncio.gather(*(one(u) for u in users)))


def seat_labels(n: int) -> list[str]:
    # Rows A..Z, AA.. with 50 seats per row: A1..A50, B1..B50, ...
    out = []
    row = 0
    while len(out) < n:
        name = ""
        r = row
        while True:
            name = chr(65 + r % 26) + name
            r = r // 26 - 1
            if r < 0:
                break
        out.extend(f"{name}{i}" for i in range(1, 51))
        row += 1
    return out[:n]


# ----------------------------------------------------------------------------- scenarios


async def reserve(api: Api, scenario: str, user: str, h: dict, show_id: str, seats: list[str],
                  key: str | None = None, hold: bool = False, extra: dict | None = None) -> Result:
    key = key or uuid.uuid4().hex
    payload = {"seats": seats, "idempotency_key": key, "hold": hold, **(extra or {})}
    status, body, lat = await api.call("POST", f"/shows/{show_id}/reserve", headers=h, json_body=payload)
    return Result(scenario, user, status, err_code(body), lat, body, key)


async def hold_then(api: Api, user: str, h: dict, show_id: str, seat: str, action: str) -> list[Result]:
    r = await reserve(api, "hold", user, h, show_id, [seat], hold=True)
    out = [r]
    if r.status == 201:
        status, body, lat = await api.call("POST", f"/reservations/{r.reservation_id}/{action}", headers=h)
        out.append(Result(f"hold-{action}", user, status, err_code(body), lat, body))
    return out


async def monitor_invariant(api: Api, show_id: str, stop: asyncio.Event, samples: list):
    while not stop.is_set():
        try:
            r = await api.client.get(f"/shows/{show_id}", timeout=30)
            if r.status_code == 200:
                c = r.json()["counts"]
                samples.append((c["available"] + c["held"] + c["confirmed"] == c["total"], c))
        except httpx.HTTPError:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


# ----------------------------------------------------------------------------- metrics


async def scrape(api: Api) -> dict[str, float]:
    try:
        r = await api.client.get("/metrics", timeout=30)
    except httpx.HTTPError:
        return {}
    out = {}
    for line in r.text.splitlines():
        if line and not line.startswith("#"):
            k, _, v = line.rpartition(" ")
            try:
                out[k] = float(v)
            except ValueError:
                pass
    return out


# ----------------------------------------------------------------------------- main


async def run(args) -> int:
    api = Api(args.base_url, args.concurrency, args.timeout, args.transport_retries)
    run_id = uuid.uuid4().hex[:6]
    rng = random.Random(args.seed)
    print(f"burst run {run_id} -> {api.base}")

    await wait_ready(api, args.ready_timeout)

    # ---- layout: dedicated seat ranges per scenario so expectations are exact
    labels = seat_labels(args.seats)
    hot_seats = labels[: args.hot_seats]
    cursor = args.hot_seats
    limit_block = labels[cursor: cursor + args.limit_users * 10]
    cursor += len(limit_block)
    idem_seats = labels[cursor: cursor + args.idem_users]
    cursor += len(idem_seats)
    hold_seats = labels[cursor: cursor + args.hold_users]
    cursor += len(hold_seats)
    general_seats = labels[cursor:]
    if len(general_seats) < 10:
        sys.exit("--seats too small for the configured scenarios")

    status, show, _ = await api.call(
        "POST", "/shows",
        headers={"Authorization": f"Bearer {args.admin_token}"},
        json_body={"name": f"burst-{run_id}", "seats": labels, "price_paise": args.price_paise,
                   "per_user_limit": args.per_user_limit},
    )
    if status != 201:
        sys.exit(f"create show failed: {status} {show}")
    show_id = show["id"]
    limit = show["per_user_limit"]
    print(f"show {show_id}: {len(labels)} seats, per_user_limit={limit}, price={args.price_paise} paise")

    hot_users = {s: [f"hot-{run_id}-{s}-{i}" for i in range(args.hot_users)] for s in hot_seats}
    limit_users = [f"lim-{run_id}-{i}" for i in range(args.limit_users)]
    idem_users = [f"idem-{run_id}-{i}" for i in range(args.idem_users)]
    hold_users = [f"hold-{run_id}-{i}" for i in range(args.hold_users)]
    gen_users = [f"gen-{run_id}-{i}" for i in range(args.general_users)]
    spoof_users = [f"spoof-{run_id}-a", f"spoof-{run_id}-b"]
    all_users = [u for us in hot_users.values() for u in us] + limit_users + idem_users + hold_users + gen_users + spoof_users

    t0 = time.perf_counter()
    tokens = await mint_tokens(api, all_users)
    print(f"minted {len(tokens)} user tokens in {time.perf_counter() - t0:.1f}s")

    # ---- build the stampede
    jobs = []
    for seat, users in hot_users.items():
        jobs += [reserve(api, "hot-seat", u, tokens[u], show_id, [seat]) for u in users]
    for i, u in enumerate(limit_users):
        jobs += [reserve(api, "limit", u, tokens[u], show_id, [s]) for s in limit_block[i * 10:(i + 1) * 10]]
    idem_keys = {u: uuid.uuid4().hex for u in idem_users}
    for u, s in zip(idem_users, idem_seats):
        jobs += [reserve(api, "idempotent", u, tokens[u], show_id, [s], key=idem_keys[u]) for _ in range(args.idem_retries)]
    hold_jobs = [hold_then(api, u, tokens[u], show_id, s, "confirm" if i % 2 == 0 else "cancel")
                 for i, (u, s) in enumerate(zip(hold_users, hold_seats))]

    fixed = len(jobs) + len(hold_jobs)
    n_general = max(0, args.requests - fixed)
    for _ in range(n_general):
        u = rng.choice(gen_users)
        seats = rng.sample(general_seats, rng.choice((1, 1, 1, 2)))
        if rng.random() < args.retry_ratio:
            # A client retry: same key, same body, sent twice concurrently.
            k = uuid.uuid4().hex
            jobs.append(reserve(api, "general", u, tokens[u], show_id, seats, key=k))
            jobs.append(reserve(api, "general-retry", u, tokens[u], show_id, seats, key=k))
        else:
            jobs.append(reserve(api, "general", u, tokens[u], show_id, seats))
    rng.shuffle(jobs)

    metrics_before = await scrape(api)
    stop = asyncio.Event()
    samples: list = []
    monitor = asyncio.create_task(monitor_invariant(api, show_id, stop, samples))

    total_reqs = len(jobs) + len(hold_jobs)
    print(f"firing ~{total_reqs} reserve requests (+ confirms/cancels) with concurrency {args.concurrency} ...", flush=True)
    t0 = time.perf_counter()
    gathered = await asyncio.gather(*jobs, *hold_jobs)
    elapsed = time.perf_counter() - t0
    stop.set()
    await monitor

    results: list[Result] = []
    for g in gathered:
        results.extend(g if isinstance(g, list) else [g])

    # ---- post-burst probes
    post: list[Result] = []
    for i, u in enumerate(idem_users[:20]):  # same key, different seats
        other = idem_seats[(i + 1) % len(idem_seats)]
        post.append(await reserve(api, "idem-reuse", u, tokens[u], show_id, [other], key=idem_keys[u]))
    a, b = spoof_users
    spoof = await reserve(api, "spoof", a, tokens[a], show_id, [general_seats[-1]], extra={"user_id": b})
    if spoof.status == 409:  # seat got taken in the stampede; find a free one
        post.append(spoof)
        state = (await api.client.get(f"/shows/{show_id}")).json()
        free = next((x["seat"] for x in state["seats"] if x["status"] == "available"), None)
        if free:
            spoof = await reserve(api, "spoof", a, tokens[a], show_id, [free], extra={"user_id": b})
    spoof_cancel = spoof_get = None
    if spoof.status == 201:
        spoof_cancel, _, _ = await api.call("POST", f"/reservations/{spoof.reservation_id}/cancel", headers=tokens[b])
        spoof_get, _, _ = await api.call("GET", f"/reservations/{spoof.reservation_id}", headers=tokens[b])
    post.append(spoof)

    await asyncio.sleep(1.5)  # let /metrics see the final state
    final = (await api.client.get(f"/shows/{show_id}", timeout=60)).json()
    metrics_after = await scrape(api)
    await api.close()

    # ------------------------------------------------------------------ report
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = ""):
        checks.append((name, ok, detail))

    everything = results + post
    by_status = Counter(r.status for r in everything)
    outcome = Counter()
    for r in everything:
        if r.status in (200, 201):
            outcome["confirmed" if r.status == 201 and r.body.get("status") == "confirmed"
                    else "held" if r.status == 201 else "idempotent_replay"] += 1
        elif r.status == 0:
            outcome["transport_error"] += 1
        elif r.status >= 500:
            outcome["5xx"] += 1
        else:
            outcome[f"declined:{r.code}"] += 1

    print(f"\n=== burst finished in {elapsed:.1f}s  ({len(results) / elapsed:.0f} req/s) ===")
    print("\nHTTP status distribution")
    for s, n in sorted(by_status.items()):
        print(f"  {s or 'transport-error':>16}: {n}")
    print("\nOutcome distribution")
    for k, n in sorted(outcome.items(), key=lambda kv: -kv[1]):
        print(f"  {k:>40}: {n}")
    print("\nBy scenario")
    per_scen = defaultdict(Counter)
    for r in everything:
        per_scen[r.scenario][r.status if r.status < 400 else f"{r.status}:{r.code}"] += 1
    for scen, c in per_scen.items():
        print(f"  {scen:>14}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items(), key=str)))
    lats = sorted(r.latency_s for r in results if r.status)
    if lats:
        q = statistics.quantiles(lats, n=100)
        print(f"\nLatency  p50={q[49] * 1000:.0f}ms  p95={q[94] * 1000:.0f}ms  p99={q[98] * 1000:.0f}ms  max={lats[-1] * 1000:.0f}ms")
    print(f"Client transport retries: {api.transport_errors}")

    # 1. hot seats: exactly one winner each
    hot_ok, hot_detail = True, []
    for seat in hot_seats:
        users = set(hot_users[seat])
        rs = [r for r in results if r.scenario == "hot-seat" and r.user in users]
        wins = sum(r.status == 201 for r in rs)
        others = Counter(r.status for r in rs if r.status != 201)
        ok = wins == 1 and set(others) <= {409}
        hot_ok &= ok
        hot_detail.append(f"{seat}: 201x{wins} 409x{others.get(409, 0)}" + ("" if ok else f" other={dict(others)}"))
    check("hot seats: exactly one 201 each, rest 409", hot_ok, "; ".join(hot_detail))

    # 2. zero 5xx
    n5 = sum(1 for r in everything if r.status >= 500)
    check("zero 5xx", n5 == 0, f"{n5} x 5xx")
    check("zero unrecovered transport errors", by_status.get(0, 0) == 0,
          f"{by_status.get(0, 0)} requests failed at the transport layer after retries")

    # 3. invariant during and after
    bad = [c for ok, c in samples if not ok]
    check("available+held+confirmed==total during burst", not bad, f"{len(samples)} samples, {len(bad)} violations")
    fc = final["counts"]
    check("available+held+confirmed==total after burst",
          fc["available"] + fc["held"] + fc["confirmed"] == fc["total"] == len(labels), json.dumps(fc))

    # 4. idempotency
    idem_bad = []
    for u in idem_users:
        rs = [r for r in results if r.scenario == "idempotent" and r.user == u]
        ids = {r.reservation_id for r in rs if r.status in (200, 201)}
        created = sum(r.status == 201 for r in rs)
        if len(ids) != 1 or created != 1 or any(r.status not in (200, 201) for r in rs):
            idem_bad.append(f"{u}: 201x{created} ids={len(ids)} statuses={Counter(r.status for r in rs)}")
    check("idempotent retries: one reservation per key", not idem_bad,
          f"{len(idem_users)} keys x {args.idem_retries} sends" + (f"; bad: {idem_bad[:3]}" if idem_bad else ""))
    reuse = [r for r in post if r.scenario == "idem-reuse"]
    check("same key + different seats -> 409 idempotency_key_reused",
          all(r.status == 409 and r.code == "idempotency_key_reused" for r in reuse), f"{Counter((r.status, r.code) for r in reuse)}")

    # 5. per-user limit
    over = {u: n for u in limit_users if (n := sum(r.status == 201 for r in results if r.user == u)) > limit}
    exact = sum(1 for u in limit_users if sum(r.status == 201 for r in results if r.user == u) == limit)
    check(f"per-user limit: nobody above {limit}", not over, f"{exact}/{len(limit_users)} users at exactly {limit}; over: {over}")
    gen_over = {}
    for u in gen_users:
        live = sum(len(r.body["seats"]) for r in results if r.user == u and r.status == 201)
        if live > limit:
            gen_over[u] = live
    check("per-user limit across general traffic", not gen_over, f"over: {dict(list(gen_over.items())[:5])}")

    # 6. identity
    spoof_ok = spoof.status == 201 and spoof.body.get("user_id") == a and spoof_cancel == 404 and spoof_get == 404
    check("identity from token: body user_id ignored, others can't cancel/view",
          spoof_ok, f"reserve={spoof.status} user_id={spoof.body.get('user_id')} other-cancel={spoof_cancel} other-get={spoof_get}")

    # 7. exact final seat map vs. what the responses say
    cancelled = {r.body["reservation_id"] for r in results if r.scenario == "hold-cancel" and r.status == 200}
    confirmed_via_hold = {r.body["reservation_id"] for r in results if r.scenario == "hold-confirm" and r.status == 200}
    created = {}
    for r in everything:
        if r.status == 201:
            created[r.reservation_id] = r.body
    expected_confirmed, expected_held, dup = set(), set(), []
    for rid, body in created.items():
        if rid in cancelled:
            continue
        target = expected_confirmed if body["status"] == "confirmed" or rid in confirmed_via_hold else expected_held
        for s in body["seats"]:
            if s in expected_confirmed or s in expected_held:
                dup.append(s)
            target.add(s)
    check("no seat granted to two live reservations", not dup, f"double-granted: {dup[:10]}")
    actual_confirmed = {s["seat"] for s in final["seats"] if s["status"] == "confirmed"}
    actual_held = {s["seat"] for s in final["seats"] if s["status"] == "held"}
    check("final seat map == sum of successful responses",
          actual_confirmed == expected_confirmed and actual_held == expected_held,
          f"confirmed api={len(actual_confirmed)} expected={len(expected_confirmed)}; "
          f"held api={len(actual_held)} expected={len(expected_held)}; "
          f"missing={sorted(expected_confirmed - actual_confirmed)[:5]} extra={sorted(actual_confirmed - expected_confirmed)[:5]}")

    # 8. metrics reconcile with the API
    if metrics_after:
        g = {st: metrics_after.get(f'seats{{show_id="{show_id}",status="{st}"}}') for st in ("available", "held", "confirmed", "total")}
        check("metrics seat gauges == GET /shows/{id}",
              g == {k: float(fc[k]) for k in g}, f"metrics={g} api={fc}")
        viol = {k: v for k, v in metrics_after.items() if k.startswith("seat_invariant_violations") and v}
        check("metrics: seat_invariant_violations all zero", not viol, str(viol))

        def delta(name):
            return metrics_after.get(name, 0) - metrics_before.get(name, 0)

        print("\nMetrics deltas over the burst (single instance & no other traffic => should match client view)")
        client_view = {
            "reservations_confirmed_total": sum(1 for r in everything if r.status == 201 and r.body.get("status") == "confirmed")
            + len(confirmed_via_hold),
            'reservations_declined_total{reason="seat_taken"}': sum(1 for r in everything if r.code == "seat_unavailable"),
            'reservations_declined_total{reason="per_user_limit"}': sum(1 for r in everything if r.code == "per_user_limit_exceeded"),
            'reservations_declined_total{reason="idempotent_replay"}': sum(1 for r in everything if r.status == 200 and r.scenario in ("idempotent", "general", "general-retry")),
            'reservations_declined_total{reason="idempotency_key_reused"}': sum(1 for r in everything if r.code == "idempotency_key_reused"),
        }
        for name, seen in client_view.items():
            d = delta(name)
            mark = "ok" if int(d) == seen else "differs"
            print(f"  {name:<62} server={int(d):>6}  client={seen:>6}  {mark}")
    else:
        check("metrics endpoint reachable", False, "/metrics scrape failed")

    print("\nChecks")
    for name, ok, detail in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    print(f"\nFinal seat counts: {json.dumps(fc)}")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"burst-{run_id}.json"
    out.write_text(json.dumps({
        "run_id": run_id, "base_url": api.base, "show_id": show_id, "elapsed_s": elapsed,
        "status_distribution": {str(k): v for k, v in by_status.items()},
        "outcomes": dict(outcome), "final_counts": fc,
        "checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in checks],
    }, indent=2))
    print(f"summary written to {out}")
    failed = [n for n, ok, _ in checks if not ok]
    print("\nRESULT:", "ALL CHECKS PASSED" if not failed else f"{len(failed)} CHECK(S) FAILED")
    return 0 if not failed else 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base_url")
    p.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "dev-admin-token"))
    p.add_argument("--requests", type=int, default=20000, help="approximate total reserve requests")
    p.add_argument("--concurrency", type=int, default=500, help="max in-flight HTTP requests")
    p.add_argument("--seats", type=int, default=2000)
    p.add_argument("--per-user-limit", type=int, default=4)
    p.add_argument("--price-paise", type=int, default=25000)
    p.add_argument("--hot-seats", type=int, default=5)
    p.add_argument("--hot-users", type=int, default=500, help="distinct users storming EACH hot seat")
    p.add_argument("--limit-users", type=int, default=50, help="users each firing 10 parallel reserves")
    p.add_argument("--idem-users", type=int, default=100)
    p.add_argument("--idem-retries", type=int, default=20, help="parallel sends of the same key per idem user")
    p.add_argument("--hold-users", type=int, default=100)
    p.add_argument("--general-users", type=int, default=2000)
    p.add_argument("--retry-ratio", type=float, default=0.1, help="share of general requests sent twice with one key")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--transport-retries", type=int, default=2)
    p.add_argument("--ready-timeout", type=float, default=180.0, help="cold-start budget for /readyz")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out-dir", default="burst-results")
    p.add_argument("--quick", action="store_true", help="small preset for a local smoke run")
    args = p.parse_args()
    if args.quick:
        args.requests, args.concurrency, args.seats = 2000, 100, 400
        args.hot_users, args.limit_users, args.idem_users, args.hold_users, args.general_users = 100, 10, 20, 20, 200
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
