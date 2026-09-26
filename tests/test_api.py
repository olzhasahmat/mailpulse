import time

import pytest
from httpx import ASGITransport, AsyncClient

from mailpulse.api.auth import build_init_data
from mailpulse.api.deps import get_bot, get_sessionmaker
from mailpulse.api.main import create_app
from mailpulse.config import Settings, get_settings
from mailpulse.db import models

pytestmark = pytest.mark.db


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str) -> None:
        self.sent.append((chat_id, text))


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
    fake_bot = FakeBot()
    app.dependency_overrides[get_bot] = lambda: fake_bot
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        c.fake_bot = fake_bot
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


async def _seed_account(db, user_id: int, *, status=models.AccountStatus.ACTIVE) -> int:
    async with db.begin() as session:
        account = models.MailAccount(
            user_id=user_id,
            provider=models.Provider.GMAIL,
            email="you@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
            status=status,
        )
        session.add(account)
        await session.flush()
        message = models.Message(
            account_id=account.id,
            folder="INBOX",
            uid=1,
            message_id_hdr="<m@x>",
            from_addr="a@b.c",
            subject="Счёт",
        )
        session.add(message)
        await session.flush()
        return account.id


async def _user_id(db, tg_user_id: int) -> int:
    from sqlalchemy import select

    async with db() as session:
        return await session.scalar(
            select(models.User.id).where(models.User.tg_user_id == tg_user_id)
        )


async def test_pause_and_resume_toggle_status(client, db):
    await client.get("/api/accounts", headers=auth_header(tg_user_id=501))
    account_id = await _seed_account(db, await _user_id(db, 501))

    assert (
        await client.post(f"/api/accounts/{account_id}/pause", headers=auth_header(501))
    ).status_code == 204
    async with db() as session:
        assert (
            await session.get(models.MailAccount, account_id)
        ).status == models.AccountStatus.DISABLED

    assert (
        await client.post(f"/api/accounts/{account_id}/resume", headers=auth_header(501))
    ).status_code == 204
    async with db() as session:
        assert (
            await session.get(models.MailAccount, account_id)
        ).status == models.AccountStatus.ACTIVE


async def test_delete_removes_account_and_cascades_messages(client, db):
    from sqlalchemy import func, select

    await client.get("/api/accounts", headers=auth_header(tg_user_id=502))
    account_id = await _seed_account(db, await _user_id(db, 502))

    resp = await client.delete(f"/api/accounts/{account_id}", headers=auth_header(502))

    assert resp.status_code == 204
    async with db() as session:
        assert await session.get(models.MailAccount, account_id) is None
        # каскад: письма удалены вместе с ящиком
        assert await session.scalar(select(func.count()).select_from(models.Message)) == 0
    # после удаления бот прислал напоминание отозвать пароль приложения
    assert len(client.fake_bot.sent) == 1
    _, text = client.fake_bot.sent[0]
    assert "отзовите пароль" in text.lower()
    assert "myaccount.google.com/apppasswords" in text


async def test_cannot_touch_another_users_account(client, db):
    await client.get("/api/accounts", headers=auth_header(tg_user_id=503))
    account_id = await _seed_account(db, await _user_id(db, 503))

    assert (
        await client.post(f"/api/accounts/{account_id}/pause", headers=auth_header(999))
    ).status_code == 404
    assert (
        await client.delete(f"/api/accounts/{account_id}", headers=auth_header(999))
    ).status_code == 404
