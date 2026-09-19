"""Импорт архива: письма прошлого сохраняются, попадают в треды, историю и RAG-индекс.

Классификация и карточки не запускаются: архив нужен как контекст, а не как поток уведомлений.
Работает и без ключей LLM — индексацию делает воркер.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.db.models import Message
from mailpulse.mail.imap import ImapMailbox
from mailpulse.mail.sync import AccountRef, save_message

log = logging.getLogger(__name__)

FETCH_BATCH = 50


@dataclass(frozen=True)
class ImportStats:
    seen: int = 0
    skipped_existing: int = 0
    saved: int = 0

    def merge(self, other: "ImportStats") -> "ImportStats":
        return ImportStats(
            self.seen + other.seen,
            self.skipped_existing + other.skipped_existing,
            self.saved + other.saved,
        )


async def existing_message_ids(
    session: AsyncSession, account_id: int, message_ids: list[str]
) -> set[str]:
    if not message_ids:
        return set()
    stmt = select(Message.message_id_hdr).where(
        Message.account_id == account_id, Message.message_id_hdr.in_(message_ids)
    )
    return set((await session.execute(stmt)).scalars())


async def import_folder(
    mailbox: ImapMailbox,
    sessionmaker: async_sessionmaker[AsyncSession],
    account: AccountRef,
    attachments_dir: Path,
    folder: str,
    since: date,
) -> ImportStats:
    """Импортирует письма папки начиная с даты since. Уже сохранённые пропускает по Message-ID."""
    await asyncio.to_thread(mailbox.select, folder)
    uids = await asyncio.to_thread(mailbox.uids_since, since)
    stats = ImportStats(seen=len(uids))

    for start in range(0, len(uids), FETCH_BATCH):
        batch = uids[start : start + FETCH_BATCH]
        # Сначала дешёвые Message-ID: не качаем тела писем, которые уже в базе
        ids_by_uid = await asyncio.to_thread(mailbox.message_ids, batch)
        async with sessionmaker() as session:
            known = await existing_message_ids(
                session, account.id, [mid for mid in ids_by_uid.values() if mid]
            )
        to_fetch = [uid for uid, mid in ids_by_uid.items() if mid is None or mid not in known]
        stats = stats.merge(ImportStats(skipped_existing=len(batch) - len(to_fetch)))
        if not to_fetch:
            continue

        raw_by_uid = await asyncio.to_thread(mailbox.fetch_raw, to_fetch)
        async with sessionmaker.begin() as session:
            for uid in to_fetch:
                raw = raw_by_uid.get(uid)
                if raw is not None and await save_message(
                    session, account, folder, uid, raw, attachments_dir, enqueue_processing=False
                ):
                    stats = stats.merge(ImportStats(saved=1))
    log.info(
        "account %s %s: seen %d, saved %d, skipped %d",
        account.id,
        folder,
        stats.seen,
        stats.saved,
        stats.skipped_existing,
    )
    return stats
