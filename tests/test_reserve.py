import asyncio
import uuid
from collections import Counter

import pytest

from app import reservations
from tests.conftest import assert_reconciled


def key() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def no_precheck(monkeypatch):
    """Disable the non-locking early decline so the locked UPDATE alone must be correct."""

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(reservations, "_precheck_seats", _noop)


async def test_reserve_happy_path(client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("alice")
    r = await client.post(f"/shows/{show['id']}/reserve", json={"seats": ["A12"], "idempotency_key": key()}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user_id"] == "alice"
    assert body["seats"] == ["A12"]
    assert body["amount_paise"] == 25000
    assert body["status"] == "confirmed"
    assert body["show_id"] == show["id"]

    state = (await client.get(f"/shows/{show['id']}")).json()
    assert state["counts"] == {"total": 20, "available": 19, "held": 0, "confirmed": 1}
    assert next(s for s in state["seats"] if s["seat"] == "A12")["status"] == "confirmed"


async def test_requires_valid_token(client, make_show):
    show = await make_show()
    url = f"/shows/{show['id']}/reserve"
    body = {"seats": ["A1"], "idempotency_key": key()}
    assert (await client.post(url, json=body)).status_code == 401
    assert (await client.post(url, json=body, headers={"Authorization": "Bearer garbage"})).status_code == 401


async def test_identity_comes_from_token_not_body(app, client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("mallory")
    r = await client.post(
        f"/shows/{show['id']}/reserve",
        json={"seats": ["A1"], "idempotency_key": key(), "user_id": "victim"},
        headers=h,
    )
    assert r.status_code == 201
    assert r.json()["user_id"] == "mallory"
    async with app.state.pool.acquire() as conn:
        assert await conn.fetchval("SELECT user_id FROM seats WHERE show_id = $1 AND label = 'A1'", uuid.UUID(show["id"])) == "mallory"


async def test_validation_and_unknown_seats(client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("bob")
    url = f"/shows/{show['id']}/reserve"
    assert (await client.post(url, json={"seats": ["A1"]}, headers=h)).status_code == 422  # no key
    assert (await client.post(url, json={"seats": ["A1", "A1"], "idempotency_key": key()}, headers=h)).status_code == 422
    r = await client.post(url, json={"seats": ["Z99"], "idempotency_key": key()}, headers=h)
    assert r.status_code == 422 and r.json()["error"]["code"] == "unknown_seats"
    r = await client.post(url, json={"seats": ["A1"]}, headers={**h, "Idempotency-Key": key()})
    assert r.status_code == 201  # header form works
    r = await client.post(url, json={"seats": ["A2"], "idempotency_key": key()}, headers={**h, "Idempotency-Key": key()})
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_mismatch"


async def test_partial_request_is_all_or_nothing(client, make_show, auth_for):
    show = await make_show()
    url = f"/shows/{show['id']}/reserve"
    assert (await client.post(url, json={"seats": ["A12"], "idempotency_key": key()}, headers=await auth_for("u1"))).status_code == 201
    r = await client.post(url, json={"seats": ["A12", "A13"], "idempotency_key": key()}, headers=await auth_for("u2"))
    assert r.status_code == 409
    assert r.json()["error"] == {"code": "seat_unavailable", "message": "seat(s) already taken", "seats": ["A12"]}
    state = (await client.get(f"/shows/{show['id']}")).json()
    assert next(s for s in state["seats"] if s["seat"] == "A13")["status"] == "available"


@pytest.mark.parametrize("precheck", [True, False], ids=["with-precheck", "locked-path-only"])
async def test_hot_seat_storm_has_exactly_one_winner(app, client, make_show, auth_for, monkeypatch, precheck):
    if not precheck:
        monkeypatch.setattr(reservations, "_precheck_seats", lambda *a, **k: asyncio.sleep(0))
    show = await make_show()
    url = f"/shows/{show['id']}/reserve"
    users = [f"storm{i}" for i in range(200)]
    headers = [await auth_for(u) for u in users]

    results = await asyncio.gather(
        *(client.post(url, json={"seats": ["A12"], "idempotency_key": key()}, headers=h) for h in headers)
    )
    codes = Counter(r.status_code for r in results)
    assert codes == {201: 1, 409: 199}, codes
    assert {r.json()["error"]["code"] for r in results if r.status_code == 409} == {"seat_unavailable"}
    counts = await assert_reconciled(app, show["id"])
    assert counts["confirmed"] == 1


@pytest.mark.parametrize("precheck", [True, False], ids=["with-precheck", "locked-path-only"])
async def test_per_user_limit_under_concurrency(app, client, make_show, auth_for, monkeypatch, precheck):
    if not precheck:
        monkeypatch.setattr(reservations, "_precheck_seats", lambda *a, **k: asyncio.sleep(0))
    show = await make_show(per_user_limit=4)
    h = await auth_for("greedy")
    url = f"/shows/{show['id']}/reserve"
    results = await asyncio.gather(
        *(client.post(url, json={"seats": [f"A{i}"], "idempotency_key": key()}, headers=h) for i in range(1, 11))
    )
    codes = Counter(r.status_code for r in results)
    assert codes == {201: 4, 409: 6}, codes
    assert {r.json()["error"]["code"] for r in results if r.status_code == 409} == {"per_user_limit_exceeded"}
    counts = await assert_reconciled(app, show["id"])
    assert counts["confirmed"] == 4


async def test_request_larger_than_limit_is_declined(client, make_show, auth_for):
    show = await make_show(per_user_limit=2)
    r = await client.post(
        f"/shows/{show['id']}/reserve", json={"seats": ["A1", "A2", "A3"], "idempotency_key": key()}, headers=await auth_for("x")
    )
    assert r.status_code == 409 and r.json()["error"]["code"] == "per_user_limit_exceeded"


async def test_idempotent_retry_storm_reserves_once(app, client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("retrier")
    url = f"/shows/{show['id']}/reserve"
    k = key()
    results = await asyncio.gather(*(client.post(url, json={"seats": ["A5"], "idempotency_key": k}, headers=h) for _ in range(50)))
    codes = Counter(r.status_code for r in results)
    assert codes[201] == 1 and codes[200] == 49, codes
    assert len({r.json()["reservation_id"] for r in results}) == 1
    assert all(r.headers.get("Idempotent-Replayed") == "true" for r in results if r.status_code == 200)
    counts = await assert_reconciled(app, show["id"])
    assert counts["confirmed"] == 1

    # Same key, different seats -> rejected, nothing moves.
    r = await client.post(url, json={"seats": ["A6"], "idempotency_key": k}, headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "idempotency_key_reused"
    # Seat order is not part of identity.
    k2 = key()
    assert (await client.post(url, json={"seats": ["A7", "A8"], "idempotency_key": k2}, headers=h)).status_code == 201
    assert (await client.post(url, json={"seats": ["A8", "A7"], "idempotency_key": k2}, headers=h)).status_code == 200
    assert (await assert_reconciled(app, show["id"]))["confirmed"] == 3


async def test_idempotency_keys_are_scoped_per_user(client, make_show, auth_for):
    show = await make_show()
    url = f"/shows/{show['id']}/reserve"
    shared = key()
    r1 = await client.post(url, json={"seats": ["A1"], "idempotency_key": shared}, headers=await auth_for("p"))
    r2 = await client.post(url, json={"seats": ["A2"], "idempotency_key": shared}, headers=await auth_for("q"))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["reservation_id"] != r2.json()["reservation_id"]


async def test_overlapping_multiseat_requests_no_deadlock_no_double_sell(app, client, make_show, auth_for, no_precheck):
    show = await make_show(seats=[f"S{i}" for i in range(1, 7)], per_user_limit=6)
    url = f"/shows/{show['id']}/reserve"
    combos = [["S1", "S2", "S3"], ["S3", "S2", "S1"], ["S2", "S4"], ["S4", "S1"], ["S5", "S6", "S3"], ["S6", "S5"]]
    reqs = []
    for i in range(120):
        seats = combos[i % len(combos)]
        reqs.append(client.post(url, json={"seats": seats, "idempotency_key": key()}, headers=await auth_for(f"m{i}")))
    results = await asyncio.gather(*reqs)
    codes = Counter(r.status_code for r in results)
    assert set(codes) <= {201, 409}, codes  # zero 5xx, no deadlock errors
    won = [s for r in results if r.status_code == 201 for s in r.json()["seats"]]
    assert len(won) == len(set(won)), "a seat was sold twice"
    await assert_reconciled(app, show["id"])
