import asyncio
import uuid
from collections import Counter

from tests.conftest import assert_reconciled


def key() -> str:
    return uuid.uuid4().hex


async def _reserve(client, show_id, headers, seats, hold=False):
    return await client.post(
        f"/shows/{show_id}/reserve", json={"seats": seats, "idempotency_key": key(), "hold": hold}, headers=headers
    )


async def _seat_status(client, show_id, label):
    state = (await client.get(f"/shows/{show_id}")).json()
    return next(s["status"] for s in state["seats"] if s["seat"] == label), state["counts"]


async def _force_expire(app, reservation_id):
    async with app.state.pool.acquire() as conn:
        rid = uuid.UUID(reservation_id)
        await conn.execute("UPDATE reservations SET hold_expires_at = now() - interval '1 second' WHERE id = $1", rid)
        await conn.execute("UPDATE seats SET hold_expires_at = now() - interval '1 second' WHERE reservation_id = $1", rid)


async def test_cancel_releases_seat_and_limit(app, client, make_show, auth_for):
    show = await make_show(per_user_limit=1)
    a, b = await auth_for("c-a"), await auth_for("c-b")
    r = await _reserve(client, show["id"], a, ["A1"])
    rid = r.json()["reservation_id"]
    assert (await _reserve(client, show["id"], a, ["A2"])).status_code == 409  # at limit

    r = await client.post(f"/reservations/{rid}/cancel", headers=a)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert (await _seat_status(client, show["id"], "A1"))[0] == "available"

    # Seat is cleanly re-bookable by someone else, and the owner's limit is freed.
    assert (await _reserve(client, show["id"], b, ["A1"])).status_code == 201
    assert (await _reserve(client, show["id"], a, ["A2"])).status_code == 201
    await assert_reconciled(app, show["id"])


async def test_only_owner_can_cancel_or_view(client, make_show, auth_for):
    show = await make_show()
    owner, other = await auth_for("o-owner"), await auth_for("o-other")
    rid = (await _reserve(client, show["id"], owner, ["A1"])).json()["reservation_id"]

    assert (await client.post(f"/reservations/{rid}/cancel", headers=other)).status_code == 404
    assert (await client.get(f"/reservations/{rid}", headers=other)).status_code == 404
    assert (await client.post(f"/reservations/{rid}/cancel")).status_code == 401
    assert (await _seat_status(client, show["id"], "A1"))[0] == "confirmed"
    assert (await client.get(f"/reservations/{rid}", headers=owner)).json()["status"] == "confirmed"


async def test_concurrent_cancels_release_once(app, client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("cc")
    rid = (await _reserve(client, show["id"], h, ["A1", "A2"])).json()["reservation_id"]
    results = await asyncio.gather(*(client.post(f"/reservations/{rid}/cancel", headers=h) for _ in range(20)))
    assert Counter(r.status_code for r in results) == {200: 20}
    assert {r.json()["status"] for r in results} == {"cancelled"}
    counts = await assert_reconciled(app, show["id"])
    assert counts["available"] == counts["total"]


async def test_hold_then_confirm(app, client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("h-1")
    r = await _reserve(client, show["id"], h, ["A3"], hold=True)
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "held" and body["hold_expires_at"]
    status, counts = await _seat_status(client, show["id"], "A3")
    assert status == "held" and counts["held"] == 1

    r = await client.post(f"/reservations/{body['reservation_id']}/confirm", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "confirmed" and r.json()["hold_expires_at"] is None
    assert (await client.post(f"/reservations/{body['reservation_id']}/confirm", headers=h)).status_code == 200
    assert (await _seat_status(client, show["id"], "A3"))[0] == "confirmed"
    await assert_reconciled(app, show["id"])


async def test_hold_auto_expires_and_seat_is_rebookable(app, client, make_show, auth_for):
    show = await make_show(hold_ttl_seconds=1, per_user_limit=1)
    h, other = await auth_for("e-1"), await auth_for("e-2")
    rid = (await _reserve(client, show["id"], h, ["A4"], hold=True)).json()["reservation_id"]
    assert (await _reserve(client, show["id"], other, ["A4"])).status_code == 409

    for _ in range(30):  # ttl 1s + reaper every 0.2s
        await asyncio.sleep(0.1)
        if (await _seat_status(client, show["id"], "A4"))[0] == "available":
            break
    assert (await _seat_status(client, show["id"], "A4"))[0] == "available"
    assert (await client.get(f"/reservations/{rid}", headers=h)).json()["status"] == "expired"

    r = await client.post(f"/reservations/{rid}/confirm", headers=h)
    assert r.status_code == 409 and r.json()["error"]["code"] == "reservation_not_active"
    assert (await _reserve(client, show["id"], other, ["A4"])).status_code == 201
    assert (await _reserve(client, show["id"], h, ["A5"])).status_code == 201  # limit freed by expiry
    await assert_reconciled(app, show["id"])


async def test_confirm_after_expiry_is_declined_even_before_reaper(app, client, make_show, auth_for):
    show = await make_show()
    h = await auth_for("late")
    rid = (await _reserve(client, show["id"], h, ["A6"], hold=True)).json()["reservation_id"]
    await _force_expire(app, rid)
    r = await client.post(f"/reservations/{rid}/confirm", headers=h)
    # Either we expired it inline (hold_expired) or the reaper beat us to it.
    assert r.status_code == 409
    assert r.json()["error"]["code"] in {"hold_expired", "reservation_not_active"}
    assert (await _seat_status(client, show["id"], "A6"))[0] == "available"
    await assert_reconciled(app, show["id"])


async def test_release_never_resurrects_someone_elses_seat(app, client, make_show, auth_for):
    show = await make_show()
    first, second = await auth_for("r-1"), await auth_for("r-2")
    rid = (await _reserve(client, show["id"], first, ["A7"], hold=True)).json()["reservation_id"]
    await _force_expire(app, rid)
    async with app.state.pool.acquire() as conn:
        from app.reservations import expire_one

        while await expire_one(conn):
            pass
    assert (await _reserve(client, show["id"], second, ["A7"])).status_code == 201

    # The stale owner cancels/confirms their dead reservation: must not touch A7.
    r = await client.post(f"/reservations/{rid}/cancel", headers=first)
    assert r.status_code == 200 and r.json()["status"] == "expired"
    assert (await client.post(f"/reservations/{rid}/confirm", headers=first)).status_code == 409
    assert (await _seat_status(client, show["id"], "A7"))[0] == "confirmed"
    async with app.state.pool.acquire() as conn:
        owner = await conn.fetchval(
            "SELECT user_id FROM seats WHERE show_id = $1 AND label = 'A7'", uuid.UUID(show["id"])
        )
    assert owner == "r-2"
    await assert_reconciled(app, show["id"])


async def test_churn_reserve_cancel_expire_keeps_invariants(app, client, make_show, auth_for):
    """Reserves, holds, cancels, confirms and expiries all racing on 10 seats."""
    show = await make_show(seats=[f"C{i}" for i in range(10)], hold_ttl_seconds=1, per_user_limit=2)
    users = [await auth_for(f"churn{i}") for i in range(40)]

    async def actor(i: int):
        h = users[i]
        codes = []
        for round_ in range(5):
            seat = f"C{(i + round_) % 10}"
            r = await _reserve(client, show["id"], h, [seat], hold=(i % 2 == 0))
            codes.append(r.status_code)
            if r.status_code == 201:
                rid = r.json()["reservation_id"]
                action = (i + round_) % 3
                if action == 0:
                    codes.append((await client.post(f"/reservations/{rid}/cancel", headers=h)).status_code)
                elif action == 1:
                    codes.append((await client.post(f"/reservations/{rid}/confirm", headers=h)).status_code)
                # action 2: leave it (held ones expire via the reaper)
        return codes

    all_codes = Counter(c for codes in await asyncio.gather(*(actor(i) for i in range(40))) for c in codes)
    assert all(c < 500 for c in all_codes), all_codes
    await assert_reconciled(app, show["id"])
    await asyncio.sleep(1.6)  # let every remaining hold expire
    counts = await assert_reconciled(app, show["id"])
    assert counts["held"] == 0
