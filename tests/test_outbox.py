import pytest

from mailpulse.agent.adapters.outbox import (
    DraftNotApprovedError,
    OutgoingReply,
    SmtpOutbox,
    build_reply,
)
from mailpulse.db import models
from mailpulse.security import SecretsCipher

pytestmark = pytest.mark.db

CIPHER = SecretsCipher("WPZiQ0QEcGfGlbbYGVyxKgrazaPVZwcExy7CTyPR-Cc=")


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[OutgoingReply] = []

    async def __call__(self, reply: OutgoingReply) -> None:
        self.sent.append(reply)


async def seed(
    db, *, subject="Счёт на оплату №507", is_test=False, reply_to=None
) -> tuple[int, int]:
    headers = {"reply-to": reply_to} if reply_to else {}
    async with db.begin() as session:
        user = models.User(tg_user_id=1, tg_chat_id=1)
        session.add(user)
        await session.flush()
        account = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email="you@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=CIPHER.encrypt("app-password"),
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
            smtp_port=587,
        )
        session.add(account)
        await session.flush()
        message = models.Message(
            account_id=account.id,
            folder="INBOX",
            uid=1,
            message_id_hdr="<orig@partner.kz>",
            references=["<root@partner.kz>"],
            from_addr="a.seitkali@partner.kz",
            subject=subject,
            headers=headers,
            is_test=is_test,
        )
        session.add(message)
        await session.flush()
        draft = models.Draft(
            message_id=message.id,
            version=1,
            body="Добрый день! Оплатим сегодня.",
            status=models.DraftStatus.APPROVED,
        )
        session.add(draft)
        await session.flush()
        return draft.id, message.id


def test_build_reply_sets_threading_headers():
    message = models.Message(
        message_id_hdr="<orig@x>",
        references=["<a@x>"],
        from_addr="boss@x",
        subject="Отчёт",
        headers={},
    )
    reply = build_reply("you@gmail.com", message, "Готово")

    assert reply["To"] == "boss@x"
    assert reply["Subject"] == "Re: Отчёт"
    assert reply["In-Reply-To"] == "<orig@x>"
    assert reply["References"] == "<a@x> <orig@x>"


def test_build_reply_keeps_existing_re_and_uses_reply_to():
    message = models.Message(
        message_id_hdr="<o@x>",
        references=[],
        from_addr="noreply@x",
        subject="Re: Вопрос",
        headers={"reply-to": "human@x"},
    )
    reply = build_reply("you@gmail.com", message, "Ответ")

    assert reply["To"] == "human@x"
    assert reply["Subject"] == "Re: Вопрос"  # не «Re: Re: …»


async def test_approved_draft_is_sent_and_marked(db):
    draft_id, _ = await seed(db)
    sender = FakeSender()

    await SmtpOutbox(db, CIPHER, sender).send(draft_id)

    [reply] = sender.sent
    assert reply.host == "smtp.gmail.com" and reply.username == "you@gmail.com"
    assert reply.password == "app-password"  # расшифрован из secret_enc
    assert reply.message["To"] == "a.seitkali@partner.kz"
    async with db() as session:
        draft = await session.get(models.Draft, draft_id)
    assert draft.status == models.DraftStatus.SENT


async def test_unapproved_draft_is_refused(db):
    draft_id, _ = await seed(db)
    async with db.begin() as session:
        draft = await session.get(models.Draft, draft_id)
        draft.status = models.DraftStatus.PENDING
    sender = FakeSender()

    with pytest.raises(DraftNotApprovedError):
        await SmtpOutbox(db, CIPHER, sender).send(draft_id)

    assert sender.sent == []


async def test_test_email_marks_sent_without_smtp(db):
    draft_id, _ = await seed(db, is_test=True)
    sender = FakeSender()

    await SmtpOutbox(db, CIPHER, sender).send(draft_id)

    assert sender.sent == []  # тестовое письмо наружу не уходит
    async with db() as session:
        draft = await session.get(models.Draft, draft_id)
    assert draft.status == models.DraftStatus.SENT
