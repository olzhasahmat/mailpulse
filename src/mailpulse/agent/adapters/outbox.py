"""Outbox на SMTP: отправляет одобренный черновик ответа в исходный тред.

Тестовые письма (is_test) наружу не уходят — черновик помечается отправленным без SMTP.
"""

import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

import aiosmtplib
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.db import models as db
from mailpulse.security import SecretsCipher

log = logging.getLogger(__name__)


class DraftNotApprovedError(RuntimeError):
    pass


@dataclass(frozen=True)
class OutgoingReply:
    host: str
    port: int
    username: str
    password: str
    message: EmailMessage


SmtpSender = Callable[[OutgoingReply], Awaitable[None]]


async def send_via_aiosmtplib(reply: OutgoingReply) -> None:
    # Порт 465 — SSL сразу, 587 — STARTTLS. Так работают Gmail и Яндекс
    use_ssl = reply.port == 465
    await aiosmtplib.send(
        reply.message,
        hostname=reply.host,
        port=reply.port,
        username=reply.username,
        password=reply.password,
        use_tls=use_ssl,
        start_tls=not use_ssl,
        tls_context=ssl.create_default_context(),
        timeout=30,
    )


def build_reply(account_email: str, message: db.Message, body: str) -> EmailMessage:
    reply = EmailMessage()
    reply["From"] = account_email
    reply["To"] = message.headers.get("reply-to") or message.from_addr
    subject = message.subject or ""
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    reply["Message-ID"] = make_msgid(domain="mailpulse")
    reply["In-Reply-To"] = message.message_id_hdr
    reply["References"] = " ".join([*message.references, message.message_id_hdr])
    reply["Date"] = formatdate(localtime=True)
    reply.set_content(body)
    return reply


class SmtpOutbox:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        cipher: SecretsCipher,
        sender: SmtpSender = send_via_aiosmtplib,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._cipher = cipher
        self._sender = sender

    async def send(self, draft_id: int) -> None:
        async with self._sessionmaker() as session:
            draft = await session.get(db.Draft, draft_id)
            if draft is None:
                raise LookupError(f"черновик {draft_id} не найден")
            if draft.status != db.DraftStatus.APPROVED:
                # Защита в глубину: одобрение ставит только нажатие «Отправить» в Telegram
                raise DraftNotApprovedError(
                    f"черновик {draft_id} не одобрен ({draft.status.value})"
                )
            message = await session.get(db.Message, draft.message_id)
            account = await session.get(db.MailAccount, message.account_id)
            reply = build_reply(account.email, message, draft.body)
            secret = self._cipher.decrypt(account.secret_enc)
            is_test = message.is_test

        if is_test:
            log.info("draft %s: тестовое письмо, SMTP пропущен", draft_id)
        else:
            await self._sender(
                OutgoingReply(
                    host=account.smtp_host,
                    port=account.smtp_port,
                    username=account.email,
                    password=secret,
                    message=reply,
                )
            )
            log.info("draft %s: отправлено на %s", draft_id, reply["To"])

        async with self._sessionmaker.begin() as session:
            await session.execute(
                db.Draft.__table__.update()
                .where(db.Draft.id == draft_id)
                .values(status=db.DraftStatus.SENT)
            )
