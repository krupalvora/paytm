import asyncio
import json
import logging
import uuid

from prometheus_client import REGISTRY

from app.observability import JsonFormatter
from tests.conftest import assert_reconciled


def key() -> str:
    return uuid.uuid4().hex


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def parse_metrics(text: str) -> dict[str, float]:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            k, v = line.rsplit(" ", 1)
            out[k] = float(v)
    return out


async def test_counters_track_outcomes_and_gauges_reconcile(app, client, make_show, auth_for):
    show = await make_show(per_user_limit=1)
    url = f"/shows/{show['id']}/reserve"
    before = {
        "confirmed": sample("reservations_confirmed_total"),
        "seat_taken": sample("reservations_declined_total", reason="seat_taken"),
        "limit": sample("reservations_declined_total", reason="per_user_limit"),
        "replay": sample("reservations_declined_total", reason="idempotent_replay"),
    }

    headers = [await auth_for(f"obs{i}") for i in range(30)]
    storm = await asyncio.gather(
        *(client.post(url, json={"seats": ["A1"], "idempotency_key": key()}, headers=h) for h in headers)
    )
    winner_idx = next(i for i, r in enumerate(storm) if r.status_code == 201)
    winner = headers[winner_idx]
    # Winner retries (replay) and then tries to exceed the limit of 1.
    k = key()
    loser = headers[(winner_idx + 1) % 30]
    assert (await client.post(url, json={"seats": ["A2"], "idempotency_key": k}, headers=loser)).status_code == 201
    assert (await client.post(url, json={"seats": ["A2"], "idempotency_key": k}, headers=loser)).status_code == 200
    assert (await client.post(url, json={"seats": ["A3"], "idempotency_key": key()}, headers=winner)).status_code == 409

    assert sample("reservations_confirmed_total") - before["confirmed"] == 2
    assert sample("reservations_declined_total", reason="seat_taken") - before["seat_taken"] == 29
    assert sample("reservations_declined_total", reason="per_user_limit") - before["limit"] == 1
    assert sample("reservations_declined_total", reason="idempotent_replay") - before["replay"] == 1

    r = await client.get("/metrics")
    assert r.status_code == 200
    m = parse_metrics(r.text)
    sid = show["id"]
    state = (await client.get(f"/shows/{sid}")).json()["counts"]
    assert m[f'seats_available{{show_id="{sid}"}}'] == state["available"] == 18
    assert m[f'seats{{show_id="{sid}",status="confirmed"}}'] == state["confirmed"] == 2
    assert (
        m[f'seats{{show_id="{sid}",status="available"}}']
        + m[f'seats{{show_id="{sid}",status="held"}}']
        + m[f'seats{{show_id="{sid}",status="confirmed"}}']
        == m[f'seats{{show_id="{sid}",status="total"}}']
    )
    assert m["seat_metrics_db_scrape_ok"] == 1
    for check in ("seat_without_live_reservation", "user_counter_drift", "holds_overdue_for_expiry"):
        assert m[f'seat_invariant_violations{{check="{check}"}}'] == 0
    await assert_reconciled(app, sid)


async def test_http_metrics_use_route_templates(client, make_show):
    show = await make_show()
    await client.get(f"/shows/{show['id']}")
    text = (await client.get("/metrics")).text
    assert 'route="/shows/{show_id}"' in text
    assert show["id"] not in [l for l in text.splitlines() if l.startswith("http_requests_total")].__str__()


async def test_request_id_is_echoed_or_generated(client):
    r = await client.get("/healthz", headers={"X-Request-ID": "trace-abc"})
    assert r.headers["x-request-id"] == "trace-abc"
    r = await client.get("/healthz")
    assert len(r.headers["x-request-id"]) == 32


async def test_logs_are_json_with_request_and_user_id(client, make_show, auth_for, caplog):
    show = await make_show()
    h = await auth_for("logger-user")
    with caplog.at_level(logging.INFO):
        await client.post(
            f"/shows/{show['id']}/reserve",
            json={"seats": ["A1"], "idempotency_key": key()},
            headers={**h, "X-Request-ID": "rid-123"},
        )
    lines = [json.loads(JsonFormatter().format(rec)) for rec in caplog.records if rec.name in ("reservations", "access")]
    reserve_line = next(l for l in lines if l["msg"] == "reserve succeeded")
    assert reserve_line["request_id"] == "rid-123"
    assert reserve_line["user_id"] == "logger-user"
    assert reserve_line["seats"] == ["A1"]
    access = next(l for l in lines if l["msg"] == "request" and l.get("request_id") == "rid-123")
    assert access["route"] == "/shows/{show_id}/reserve" and access["status"] == 201
    assert access["user_id"] == "logger-user" and access["duration_ms"] >= 0


async def test_unhandled_error_is_json_500_with_request_id(app, client, monkeypatch):
    from app import shows

    async def boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(shows, "get_show", boom)
    r = await client.get(f"/shows/{uuid.uuid4()}", headers={"X-Request-ID": "rid-500"})
    assert r.status_code == 500
    assert r.json()["error"]["request_id"] == "rid-500"
    assert r.headers["x-request-id"] == "rid-500"


async def test_db_outage_is_503_not_500(client, make_show, auth_for, monkeypatch, app):
    show = await make_show()
    h = await auth_for("outage")

    class DownPool:
        def acquire(self, *_a, **_k):
            raise ConnectionRefusedError("db down")

        def get_size(self):
            return 0

        get_idle_size = get_max_size = get_size

    monkeypatch.setattr(app.state, "pool", DownPool())
    r = await client.post(f"/shows/{show['id']}/reserve", json={"seats": ["A1"], "idempotency_key": key()}, headers=h)
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "database_unavailable"
    assert r.headers["retry-after"] == "2"
    assert (await client.get("/readyz")).status_code == 503
