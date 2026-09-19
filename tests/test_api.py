import time

import pytest
from httpx import ASGITransport, AsyncClient

from mailpulse.api.auth import build_init_data
from mailpulse.api.deps import get_sessionmaker
from mailpulse.api.main import create_app
from mailpulse.config import Settings, get_settings
from mailpulse.db import models

pytestmark = pytest.mark.db

TOKEN = "123456:test-bot-token"
SECRETS_KEY = (
    "3b8Xk2p9Qw7rY5mZ8nB4hJ7sD1fL9gV2cX0aQ6wTuo="  # валидный Fernet-ключ, только для тестов
)


def auth_header(tg_user_id: int = 42, name: str = "Olzhas") -> dict:
    init_data = build_init_data(
        TOKEN, {"id": tg_user_id, "first_name": name, "username": "u"}, auth_date=int(time.time())
    )
    return {"Authorization": f"tma {init_data}"}


@pytest.fixture
async def client(db, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("SECRETS_KEY", SECRETS_KEY)
    get_settings.cache_clear()
    app = create_app()
    app.state.sessionmaker = db  # без реального движка lifespan
    app.dependency_overrides[get_sessionmaker] = lambda: db
    app.dependency_overrides[get_settings] = lambda: Settings(
        telegram_bot_token=TOKEN, secrets_key=SECRETS_KEY
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


async def test_requires_valid_init_data(client):
    assert (await client.get("/api/accounts")).status_code == 401
    bad = {"Authorization": "tma user=%7B%22id%22%3A1%7D&auth_date=1&hash=deadbeef"}
    assert (await client.get("/api/accounts", headers=bad)).status_code == 401


async def test_first_request_creates_user(client, db):
    resp = await client.get("/api/accounts", headers=auth_header(tg_user_id=777))

    assert resp.status_code == 200
    assert resp.json() == []
    from sqlalchemy import select

    async with db() as session:
        user = (await session.execute(select(models.User))).scalar_one()
    assert user.tg_user_id == 777


async def test_rules_crud_scoped_to_user(client):
    created = await client.post(
        "/api/rules", json={"kind": "vip", "value": "Boss@X.kz"}, headers=auth_header()
    )
    assert created.status_code == 201
    rule_id = created.json()["id"]
    assert created.json()["value"] == "boss@x.kz"  # нормализуется

    other = await client.get("/api/rules", headers=auth_header(tg_user_id=99))
    assert other.json() == []  # чужие правила не видны

    listed = await client.get("/api/rules", headers=auth_header())
    assert [r["id"] for r in listed.json()] == [rule_id]

    assert (await client.delete(f"/api/rules/{rule_id}", headers=auth_header())).status_code == 204
    assert (await client.get("/api/rules", headers=auth_header())).json() == []


async def test_cannot_delete_other_users_rule(client):
    created = await client.post(
        "/api/rules", json={"kind": "mute", "value": "spam@x"}, headers=auth_header(1)
    )

    resp = await client.delete(f"/api/rules/{created.json()['id']}", headers=auth_header(2))

    assert resp.status_code == 404


async def test_invalid_rule_kind_rejected(client):
    resp = await client.post(
        "/api/rules", json={"kind": "boss", "value": "x@y"}, headers=auth_header()
    )

    assert resp.status_code == 400


async def test_stats_shape(client):
    resp = await client.get("/api/stats", headers=auth_header())

    body = resp.json()
    assert set(body) == {
        "accounts",
        "messages",
        "processed",
        "by_importance",
        "total_cost_usd",
        "recent",
    }
    assert body["accounts"] == 0
