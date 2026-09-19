from datetime import UTC, datetime

import pytest
from mcp.client import Client

from mailpulse.db import models
from mailpulse.mcp_server.pg_backend import PostgresMailbox
from mailpulse.mcp_server.server import create_server

pytestmark = pytest.mark.db


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send(self, draft_id: int) -> None:
        self.sent.append(draft_id)


async def seed(db) -> dict:
    async with db.begin() as session:
        me = models.User(tg_user_id=1, tg_chat_id=1)
        other = models.User(tg_user_id=2, tg_chat_id=2)
        session.add_all([me, other])
        await session.flush()
        my_acc = models.MailAccount(
            user_id=me.id,
            provider=models.Provider.GMAIL,
            email="you@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="i",
            smtp_host="s",
        )
        other_acc = models.MailAccount(
            user_id=other.id,
            provider=models.Provider.GMAIL,
            email="other@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="i",
            smtp_host="s",
        )
        session.add_all([my_acc, other_acc])
        await session.flush()
        thread = models.Thread(user_id=me.id, subject_norm="счёт на оплату №507")
        session.add(thread)
        await session.flush()

        invoice = models.Message(
            account_id=my_acc.id,
            thread_id=thread.id,
            folder="INBOX",
            uid=1,
            message_id_hdr="<inv@x>",
            from_addr="a.seitkali@partner.kz",
            from_name="Айгерим",
            subject="Счёт на оплату №507",
            body_text="Счёт на 320 000 тенге до 18 сентября",
            sent_at=datetime(2026, 9, 11, 16, 40, tzinfo=UTC),
            has_attachments=True,
        )
        my_reply = models.Message(
            account_id=my_acc.id,
            thread_id=thread.id,
            folder="Sent",
            uid=2,
            message_id_hdr="<reply@x>",
            from_addr="you@gmail.com",
            subject="Re: Счёт на оплату №507",
            body_text="Оплатим завтра",
            outgoing=True,
            sent_at=datetime(2026, 9, 11, 18, 0, tzinfo=UTC),
        )
        noise = models.Message(
            account_id=my_acc.id,
            folder="INBOX",
            uid=3,
            message_id_hdr="<promo@x>",
            from_addr="news@shop.kz",
            subject="Скидки",
            body_text="Промо",
            sent_at=datetime(2026, 9, 10, tzinfo=UTC),
        )
        foreign = models.Message(
            account_id=other_acc.id,
            folder="INBOX",
            uid=4,
            message_id_hdr="<f@x>",
            from_addr="a.seitkali@partner.kz",
            subject="Счёт на оплату №999",
            body_text="чужой счёт на 320 000",
            sent_at=datetime(2026, 9, 11, tzinfo=UTC),
        )
        test_msg = models.Message(
            account_id=my_acc.id,
            folder="INBOX",
            uid=5,
            message_id_hdr="<t@x>",
            from_addr="a.seitkali@partner.kz",
            subject="Счёт тестовый",
            body_text="тест 320 000",
            is_test=True,
            sent_at=datetime(2026, 9, 12, tzinfo=UTC),
        )
        session.add_all([invoice, my_reply, noise, foreign, test_msg])
        await session.flush()
        session.add_all(
            [
                models.TriageResult(
                    message_id=invoice.id,
                    importance=3,
                    category="finance",
                    needs_reply=True,
                    reasoning="r",
                    extracted={"deadlines": ["2026-09-18"]},
                    model="m",
                    prompt_version="v",
                    latency_ms=1,
                    cost_usd=0,
                ),
                models.TriageResult(
                    message_id=noise.id,
                    importance=0,
                    category="newsletter",
                    needs_reply=False,
                    reasoning="r",
                    extracted={},
                    model="m",
                    prompt_version="v",
                    latency_ms=1,
                    cost_usd=0,
                ),
                models.Attachment(
                    message_id=invoice.id,
                    filename="schet.pdf",
                    mime="application/pdf",
                    size=10,
                    sha256="a",
                    storage_path="/tmp/x",
                    extracted_text="Сумма 320 000 тенге",
                    extraction_method=models.ExtractionMethod.TEXT_LAYER,
                ),
                models.UserRule(
                    user_id=me.id, kind=models.RuleKind.VIP, value="a.seitkali@partner.kz"
                ),
                models.UserRule(user_id=me.id, kind=models.RuleKind.MUTE, value="news@shop.kz"),
            ]
        )
        return {"user_id": me.id, "invoice": invoice.id, "foreign": foreign.id}


def client(db, user_id, outbox=None) -> Client:
    return Client(create_server(PostgresMailbox(db, user_id, outbox)))


async def test_search_scoped_to_user_and_excludes_test(db):
    ids = await seed(db)
    async with client(db, ids["user_id"]) as c:
        result = await c.call_tool("search_emails", {"query": "счёт 320 000"})

    hits = {h["message_id"] for h in result.structured_content["result"]}
    assert ids["invoice"] in hits
    assert ids["foreign"] not in hits  # чужая почта не видна
    # тестовое письмо и промо (нет 320 000) не попали
    assert all(h["from_addr"] != "news@shop.kz" for h in result.structured_content["result"])


async def test_search_min_importance(db):
    ids = await seed(db)
    async with client(db, ids["user_id"]) as c:
        result = await c.call_tool("search_emails", {"query": "", "min_importance": 2})

    assert [h["message_id"] for h in result.structured_content["result"]] == [ids["invoice"]]


async def test_thread_includes_outgoing_and_attachment_text(db):
    ids = await seed(db)
    async with client(db, ids["user_id"]) as c:
        result = await c.call_tool(
            "get_thread", {"message_id": ids["invoice"], "include_attachments": True}
        )

    messages = result.structured_content["messages"]
    assert [m["outgoing"] for m in messages] == [False, True]
    assert messages[0]["attachments"][0]["text"] == "Сумма 320 000 тенге"


async def test_foreign_message_is_not_found(db):
    ids = await seed(db)
    async with client(db, ids["user_id"]) as c:
        result = await c.call_tool("get_thread", {"message_id": ids["foreign"]})

    assert result.is_error


async def test_list_pending(db):
    ids = await seed(db)
    async with client(db, ids["user_id"]) as c:
        result = await c.call_tool("list_pending", {})

    items = result.structured_content["result"]
    assert [i["message_id"] for i in items] == [ids["invoice"]]
    assert items[0]["reason"] == "ждёт ответа"


async def test_draft_cannot_be_sent_without_telegram_approval(db):
    ids = await seed(db)
    outbox = FakeOutbox()
    async with client(db, ids["user_id"], outbox) as c:
        created = await c.call_tool(
            "create_draft", {"message_id": ids["invoice"], "body": "Оплатим сегодня"}
        )
        draft_id = created.structured_content["draft_id"]
        refused = await c.call_tool("send_draft", {"draft_id": draft_id})

    assert created.structured_content["status"] == "pending"
    assert refused.is_error  # через MCP одобрить нельзя
    assert outbox.sent == []


async def test_approved_draft_is_sent(db):
    ids = await seed(db)
    outbox = FakeOutbox()
    async with client(db, ids["user_id"], outbox) as c:
        created = await c.call_tool(
            "create_draft", {"message_id": ids["invoice"], "body": "Оплатим"}
        )
        draft_id = created.structured_content["draft_id"]
    async with db.begin() as session:
        draft = await session.get(models.Draft, draft_id)
        draft.status = models.DraftStatus.APPROVED  # как будто одобрено в Telegram
    async with client(db, ids["user_id"], outbox) as c:
        sent = await c.call_tool("send_draft", {"draft_id": draft_id})

    assert not sent.is_error
    assert sent.structured_content["status"] == "sent"
    assert outbox.sent == [draft_id]


async def test_rules_resource_lists_user_rules(db):
    ids = await seed(db)
    async with client(db, ids["user_id"]) as c:
        result = await c.read_resource("mailpulse://rules")

    text = result.contents[0].text
    assert "a.seitkali@partner.kz" in text
    assert "news@shop.kz" in text
