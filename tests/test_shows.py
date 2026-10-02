async def test_create_and_get_show(client, admin_headers):
    r = await client.post(
        "/shows",
        json={"name": "friday-night", "seats": ["A1", "A2", "A10"], "price_paise": 25000},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    show = r.json()
    assert show["per_user_limit"] == 4
    assert show["counts"] == {"total": 3, "available": 3, "held": 0, "confirmed": 0}
    assert [s["seat"] for s in show["seats"]] == ["A1", "A2", "A10"]  # creation order preserved
    assert all(s["status"] == "available" for s in show["seats"])

    r = await client.get(f"/shows/{show['id']}")
    assert r.status_code == 200
    assert r.json()["counts"] == show["counts"]


async def test_create_show_requires_admin(client):
    body = {"name": "x", "seats": ["A1"], "price_paise": 100}
    assert (await client.post("/shows", json=body)).status_code == 401
    r = await client.post("/shows", json=body, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 403


async def test_create_show_validation(client, admin_headers):
    cases = [
        {"name": "x", "seats": ["A1", "A1"], "price_paise": 100},  # duplicate seat
        {"name": "x", "seats": [], "price_paise": 100},  # no seats
        {"name": "x", "seats": ["A1"], "price_paise": 250.5},  # float money
        {"name": "x", "seats": ["A1"], "price_paise": "100"},  # stringly money
        {"name": "x", "seats": ["A1"], "price_paise": -1},
        {"name": "x", "seats": ["bad label!"], "price_paise": 100},
    ]
    for body in cases:
        r = await client.post("/shows", json=body, headers=admin_headers)
        assert r.status_code == 422, (body, r.text)
        assert r.json()["error"]["code"] == "validation_error"


async def test_get_unknown_show_is_404(client):
    assert (await client.get("/shows/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await client.get("/shows/not-a-uuid")).status_code == 404


async def test_readyz_ok_when_db_up(client):
    r = await client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "migrations": "ok"}
