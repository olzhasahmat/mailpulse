"""Треды: письмо попадает в переписку по In-Reply-To / References, а без них — по теме."""

import re
from datetime import datetime, timedelta

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailpulse.db.models import MailAccount, Message, Thread

# Тема «Re: Отчёт» без References склеивается только со свежим тредом: старые темы повторяются
SUBJECT_FALLBACK_WINDOW = timedelta(days=30)
REPLY_PREFIX_RE = re.compile(r"^\s*((re|fw|fwd|aw|wg|ответ|отв|пересл)(\[\d+\])?\s*:\s*)+", re.I)


def normalize_subject(subject: str) -> str:
    return REPLY_PREFIX_RE.sub("", subject).strip().casefold()


def is_reply_subject(subject: str) -> bool:
    return REPLY_PREFIX_RE.match(subject) is not None


async def resolve_thread(
    session: AsyncSession,
    *,
    user_id: int,
    subject: str,
    in_reply_to: str | None,
    references: list[str],
    sent_at: datetime,
) -> int:
    thread_id = None
    parents = [msg_id for msg_id in (in_reply_to, *reversed(references)) if msg_id]
    if parents:
        stmt = (
            select(Message.thread_id)
            .join(MailAccount, MailAccount.id == Message.account_id)
            .where(
                MailAccount.user_id == user_id,
                Message.message_id_hdr.in_(parents),
                Message.thread_id.is_not(None),
            )
            .limit(1)
        )
        thread_id = await session.scalar(stmt)

    subject_norm = normalize_subject(subject)
    if thread_id is None and subject_norm and is_reply_subject(subject):
        # Почтовые клиенты без References: ищем недавний тред с той же темой
        stmt = (
            select(Thread.id)
            .where(
                Thread.user_id == user_id,
                Thread.subject_norm == subject_norm,
                Thread.last_message_at >= sent_at - SUBJECT_FALLBACK_WINDOW,
            )
            .order_by(Thread.last_message_at.desc())
            .limit(1)
        )
        thread_id = await session.scalar(stmt)

    if thread_id is None:
        stmt = (
            insert(Thread)
            .values(user_id=user_id, subject_norm=subject_norm, last_message_at=sent_at)
            .returning(Thread.id)
        )
        return (await session.execute(stmt)).scalar_one()

    await session.execute(
        update(Thread)
        .where(Thread.id == thread_id)
        .values(last_message_at=func.greatest(Thread.last_message_at, sent_at))
    )
    return thread_id
