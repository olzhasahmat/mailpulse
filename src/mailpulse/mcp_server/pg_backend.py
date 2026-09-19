"""MailboxBackend на Postgres: почта одного пользователя для MCP-tool'ов.

Всё скоупится по user_id (через mail_accounts). send_draft отправляет только черновик,
одобренный пользователем в Telegram, — через MCP одобрить нельзя (ADR-010, ADR-025).
"""

from datetime import date, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.adapters.outbox import DraftNotApprovedError, SmtpOutbox
from mailpulse.db import models as db
from mailpulse.mcp_server.backend import DraftNotApprovedError as BackendDraftNotApproved
from mailpulse.mcp_server.backend import NotFoundError
from mailpulse.mcp_server.schemas import (
    AttachmentContent,
    DraftInfo,
    EmailHit,
    PendingItem,
    Thread,
    ThreadMessage,
)

SNIPPET_LEN = 160
SEARCH_TERMS_MAX = 8
PENDING_MIN_IMPORTANCE = 2


def _sent_at(message: db.Message) -> datetime:
    return message.sent_at or message.created_at


class PostgresMailbox:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        user_id: int,
        outbox: SmtpOutbox | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._user_id = user_id
        self._outbox = outbox

    def _own_messages(self):
        return (
            select(db.Message)
            .join(db.MailAccount, db.MailAccount.id == db.Message.account_id)
            .where(db.MailAccount.user_id == self._user_id, db.Message.is_test.is_(False))
        )

    async def search(
        self,
        query: str,
        *,
        from_addr: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        min_importance: int | None = None,
        limit: int = 10,
    ) -> list[EmailHit]:
        triage = db.TriageResult
        stmt = (
            self._own_messages()
            .add_columns(triage.importance)
            .outerjoin(triage, triage.message_id == db.Message.id)
        )
        for term in query.split()[:SEARCH_TERMS_MAX]:
            like = f"%{term}%"
            stmt = stmt.where(
                or_(
                    db.Message.subject.ilike(like),
                    db.Message.from_addr.ilike(like),
                    db.Message.from_name.ilike(like),
                    db.Message.body_text.ilike(like),
                )
            )
        if from_addr:
            stmt = stmt.where(func.lower(db.Message.from_addr) == from_addr.casefold())
        if date_from:
            stmt = stmt.where(func.date(db.Message.sent_at) >= date_from)
        if date_to:
            stmt = stmt.where(func.date(db.Message.sent_at) <= date_to)
        if min_importance is not None:
            stmt = stmt.where(triage.importance >= min_importance)
        stmt = stmt.order_by(db.Message.sent_at.desc().nulls_last(), db.Message.id.desc()).limit(
            limit
        )

        async with self._sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
        return [
            EmailHit(
                message_id=message.id,
                from_addr=message.from_addr,
                from_name=message.from_name,
                subject=message.subject,
                sent_at=_sent_at(message),
                snippet=message.body_text[:SNIPPET_LEN],
                importance=importance,
            )
            for message, importance in rows
        ]

    async def get_thread(self, message_id: int, *, include_attachments: bool = False) -> Thread:
        async with self._sessionmaker() as session:
            anchor = await self._owned(session, message_id)
            stmt = (
                self._own_messages()
                .where(db.Message.thread_id == anchor.thread_id)
                .order_by(db.Message.sent_at.asc().nulls_last(), db.Message.id.asc())
            )
            messages = list((await session.execute(stmt)).scalars())
            attachments = (
                await self._attachments_by_message(session, [m.id for m in messages])
                if include_attachments
                else {}
            )
        return Thread(
            thread_id=anchor.thread_id or 0,
            messages=[
                ThreadMessage(
                    message_id=m.id,
                    from_addr=m.from_addr,
                    from_name=m.from_name,
                    sent_at=_sent_at(m),
                    subject=m.subject,
                    body=m.body_text,
                    outgoing=m.outgoing,
                    attachments=attachments.get(m.id, []),
                )
                for m in messages
            ],
        )

    async def get_attachment(self, attachment_id: int) -> AttachmentContent:
        stmt = (
            select(db.Attachment)
            .join(db.Message, db.Message.id == db.Attachment.message_id)
            .join(db.MailAccount, db.MailAccount.id == db.Message.account_id)
            .where(db.Attachment.id == attachment_id, db.MailAccount.user_id == self._user_id)
        )
        async with self._sessionmaker() as session:
            attachment = (await session.execute(stmt)).scalar_one_or_none()
        if attachment is None:
            raise NotFoundError(f"вложение {attachment_id} не найдено")
        return _attachment_content(attachment, include_text=True)

    async def list_pending(self, *, since: datetime | None = None) -> list[PendingItem]:
        triage = db.TriageResult
        # Черновик отправлен → письмо больше не «ждёт действия»
        sent_reply = (
            select(db.Draft.message_id)
            .where(db.Draft.status == db.DraftStatus.SENT)
            .scalar_subquery()
        )
        stmt = (
            self._own_messages()
            .add_columns(triage.needs_reply, triage.extracted)
            .join(triage, triage.message_id == db.Message.id)
            .where(
                triage.importance >= PENDING_MIN_IMPORTANCE,
                db.Message.outgoing.is_(False),
                db.Message.id.not_in(sent_reply),
            )
        )
        if since is not None:
            stmt = stmt.where(db.Message.sent_at >= since)
        stmt = stmt.add_columns(triage.importance).order_by(db.Message.sent_at.asc().nulls_last())

        async with self._sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
        items = []
        for message, needs_reply, extracted, importance in rows:
            deadline = _earliest_deadline(extracted)
            if needs_reply:
                reason = "ждёт ответа"
            elif deadline:
                reason = f"срок {deadline:%d.%m}"
            else:
                reason = "важное"
            items.append(
                PendingItem(
                    message_id=message.id,
                    from_addr=message.from_addr,
                    subject=message.subject,
                    sent_at=_sent_at(message),
                    importance=importance,
                    reason=reason,
                    deadline=deadline,
                )
            )
        return items

    async def create_draft(self, message_id: int, body: str) -> DraftInfo:
        async with self._sessionmaker.begin() as session:
            await self._owned(session, message_id)
            version = await session.scalar(
                select(func.coalesce(func.max(db.Draft.version), 0) + 1).where(
                    db.Draft.message_id == message_id
                )
            )
            stmt = (
                insert(db.Draft)
                .values(message_id=message_id, version=version, body=body)
                .returning(db.Draft.id)
            )
            draft_id = (await session.execute(stmt)).scalar_one()
        return DraftInfo(draft_id=draft_id, message_id=message_id, status="pending", body=body)

    async def send_draft(self, draft_id: int) -> DraftInfo:
        stmt = (
            select(db.Draft)
            .join(db.Message, db.Message.id == db.Draft.message_id)
            .join(db.MailAccount, db.MailAccount.id == db.Message.account_id)
            .where(db.Draft.id == draft_id, db.MailAccount.user_id == self._user_id)
        )
        async with self._sessionmaker() as session:
            draft = (await session.execute(stmt)).scalar_one_or_none()
        if draft is None:
            raise NotFoundError(f"черновик {draft_id} не найден")
        if draft.status != db.DraftStatus.APPROVED:
            raise BackendDraftNotApproved(
                f"черновик {draft_id} не одобрен пользователем в Telegram; "
                "отправить можно только подтверждённый черновик"
            )
        if self._outbox is None:
            raise BackendDraftNotApproved("отправка недоступна: SMTP не настроен")
        try:
            await self._outbox.send(draft_id)
        except DraftNotApprovedError as exc:
            raise BackendDraftNotApproved(str(exc)) from exc
        async with self._sessionmaker() as session:
            draft = await session.get(db.Draft, draft_id)
        return DraftInfo(
            draft_id=draft.id, message_id=draft.message_id, status="sent", body=draft.body
        )

    async def rules(self) -> str:
        stmt = (
            select(db.UserRule.kind, db.UserRule.value)
            .where(db.UserRule.user_id == self._user_id)
            .order_by(db.UserRule.kind, db.UserRule.value)
        )
        async with self._sessionmaker() as session:
            rows = (await session.execute(stmt)).all()
        return _format_rules(rows)

    async def _owned(self, session: AsyncSession, message_id: int) -> db.Message:
        stmt = self._own_messages().where(db.Message.id == message_id)
        message = (await session.execute(stmt)).scalar_one_or_none()
        if message is None:
            raise NotFoundError(f"письмо {message_id} не найдено")
        return message

    async def _attachments_by_message(
        self, session: AsyncSession, message_ids: list[int]
    ) -> dict[int, list[AttachmentContent]]:
        if not message_ids:
            return {}
        stmt = select(db.Attachment).where(db.Attachment.message_id.in_(message_ids))
        result: dict[int, list[AttachmentContent]] = {}
        for attachment in (await session.execute(stmt)).scalars():
            result.setdefault(attachment.message_id, []).append(
                _attachment_content(attachment, include_text=True)
            )
        return result


def _attachment_content(attachment: db.Attachment, *, include_text: bool) -> AttachmentContent:
    method = attachment.extraction_method.value if attachment.extraction_method else None
    return AttachmentContent(
        attachment_id=attachment.id,
        filename=attachment.filename,
        mime=attachment.mime,
        text=attachment.extracted_text if include_text else None,
        extraction_method=method,
    )


def _earliest_deadline(extracted: dict) -> date | None:
    dates = []
    for raw in (extracted or {}).get("deadlines", []):
        try:
            dates.append(date.fromisoformat(raw[:10]))
        except (ValueError, TypeError):
            continue
    return min(dates) if dates else None


def _format_rules(rows) -> str:
    if not rows:
        return "# Правила пользователя\n\nПравил пока нет."
    groups: dict[str, list[str]] = {}
    for kind, value in rows:
        groups.setdefault(kind.value, []).append(value)
    titles = {"vip": "VIP (всегда важно)", "mute": "Заглушены", "instruction": "Указания"}
    lines = ["# Правила пользователя", ""]
    for kind in ("vip", "mute", "instruction"):
        if kind in groups:
            lines.append(f"## {titles[kind]}")
            lines += [f"- {value}" for value in groups[kind]]
            lines.append("")
    return "\n".join(lines).strip()
