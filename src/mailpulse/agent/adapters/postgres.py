"""MailStore на Postgres и выборки, связывающие письмо с пользователем."""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.state import CallMeta, EmailView, RuleHit, SenderProfile, TriageResult
from mailpulse.db import models as db

log = logging.getLogger(__name__)

NO_LLM_META: CallMeta = {
    "model": "none",
    "prompt_version": "none",
    "latency_ms": 0,
    "cost_usd": 0.0,
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_read_tokens": 0,
    "cache_write_tokens": 0,
}


async def owner_of_message(session: AsyncSession, message_id: int) -> db.User | None:
    stmt = (
        select(db.User)
        .join(db.MailAccount, db.MailAccount.user_id == db.User.id)
        .join(db.Message, db.Message.account_id == db.MailAccount.id)
        .where(db.Message.id == message_id)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


class PostgresMailStore:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def load_email(self, message_id: int) -> EmailView:
        async with self._sessionmaker() as session:
            message = await session.get(db.Message, message_id)
        if message is None:
            raise LookupError(f"письмо {message_id} не найдено")
        return {
            "message_id": message.id,
            "thread_id": message.thread_id,
            "account_id": message.account_id,
            "from_addr": message.from_addr,
            "from_name": message.from_name,
            "subject": message.subject,
            "body_text": message.body_text,
            "sent_at": message.sent_at.isoformat() if message.sent_at else None,
            "has_attachments": message.has_attachments,
            "is_test": message.is_test,
            "headers": message.headers,
        }

    async def rule_for_sender(self, user_id: int, address: str) -> RuleHit | None:
        candidates = [address.casefold(), "@" + address.rpartition("@")[2].casefold()]
        stmt = select(db.UserRule.kind).where(
            db.UserRule.user_id == user_id,
            db.UserRule.kind.in_([db.RuleKind.VIP, db.RuleKind.MUTE]),
            func.lower(db.UserRule.value).in_(candidates),
        )
        async with self._sessionmaker() as session:
            kinds = set((await session.execute(stmt)).scalars())
        # VIP сильнее mute: лишнее уведомление лучше пропущенного важного письма
        if db.RuleKind.VIP in kinds:
            return "vip"
        if db.RuleKind.MUTE in kinds:
            return "mute"
        return None

    async def sender_profile(self, user_id: int, address: str) -> SenderProfile | None:
        async with self._sessionmaker() as session:
            profile = await session.get(db.SenderProfile, (user_id, address.casefold()))
        if profile is None:
            return None
        return {
            "address": profile.address,
            "msg_count": profile.msg_count,
            "reply_count": profile.reply_count,
            "last_contact_at": profile.last_contact_at.isoformat()
            if profile.last_contact_at
            else None,
        }

    async def save_triage(self, message_id: int, triage: TriageResult) -> None:
        meta = triage.get("meta", NO_LLM_META)
        values = {
            "importance": triage["importance"],
            "category": triage["category"],
            "needs_reply": triage["needs_reply"],
            "suspicious": triage["suspicious"],
            "reasoning": triage["reasoning"],
            "extracted": triage["extracted"],
            "model": meta["model"],
            "prompt_version": meta["prompt_version"],
            "latency_ms": meta["latency_ms"],
            "cost_usd": Decimal(str(meta["cost_usd"])),
        }
        stmt = (
            insert(db.TriageResult)
            .values(message_id=message_id, **values)
            .on_conflict_do_update(index_elements=[db.TriageResult.message_id], set_=values)
        )
        async with self._sessionmaker.begin() as session:
            await session.execute(stmt)

    async def record_llm_usage(self, user_id: int, node: str, meta: CallMeta) -> None:
        async with self._sessionmaker.begin() as session:
            session.add(
                db.LlmUsage(
                    user_id=user_id,
                    node=node,
                    model=meta["model"],
                    input_tokens=meta["input_tokens"],
                    output_tokens=meta["output_tokens"],
                    cache_read_tokens=meta["cache_read_tokens"],
                    cache_write_tokens=meta["cache_write_tokens"],
                    cost_usd=Decimal(str(meta["cost_usd"])),
                )
            )

    async def enqueue_digest(self, user_id: int, message_id: int) -> None:
        # Дайджест собирается из triage_results с importance = 1; отправка по расписанию — позже
        log.debug("message %s goes to digest of user %s", message_id, user_id)

    async def save_draft(self, message_id: int, body: str, version: int) -> int:
        stmt = (
            insert(db.Draft)
            .values(message_id=message_id, body=body, version=version)
            .returning(db.Draft.id)
        )
        async with self._sessionmaker.begin() as session:
            return (await session.execute(stmt)).scalar_one()

    async def approve_draft(self, draft_id: int) -> None:
        stmt = (
            update(db.Draft)
            .where(db.Draft.id == draft_id, db.Draft.status == db.DraftStatus.PENDING)
            .values(status=db.DraftStatus.APPROVED)
        )
        async with self._sessionmaker.begin() as session:
            await session.execute(stmt)

    async def save_feedback(
        self, user_id: int, message_id: int, kind: str, value: dict[str, Any]
    ) -> None:
        async with self._sessionmaker.begin() as session:
            session.add(
                db.Feedback(
                    user_id=user_id,
                    message_id=message_id,
                    kind=db.FeedbackKind(kind),
                    value=value,
                )
            )
