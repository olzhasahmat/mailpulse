"""Синхронизация папки IMAP: новые UID → письма в БД и задачи на их обработку.

Логика не зависит от IMAP-библиотеки и работает с MailboxClient, поэтому тестируется без сети.
"""

import asyncio
import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.db.models import Attachment, MailAccount, MailboxSync, Message
from mailpulse.mail.parsing import parse_email
from mailpulse.mail.profiles import record_contact
from mailpulse.mail.threading import is_reply_subject, resolve_thread
from mailpulse.worker import queue

log = logging.getLogger(__name__)

FETCH_BATCH = 50


@dataclass(frozen=True)
class FolderState:
    uidvalidity: int
    uidnext: int


@dataclass(frozen=True)
class AccountRef:
    id: int
    user_id: int
    email: str


class MailboxClient(Protocol):
    def select(self, folder: str) -> FolderState: ...
    def uids_after(self, last_uid: int) -> list[int]: ...
    def fetch_raw(self, uids: list[int]) -> dict[int, bytes]: ...


async def sync_folder(
    client: MailboxClient,
    sessionmaker: async_sessionmaker[AsyncSession],
    account: AccountRef,
    attachments_dir: Path,
    folder: str = "INBOX",
) -> int:
    """Сохраняет новые письма папки. Возвращает число сохранённых."""
    state = await asyncio.to_thread(client.select, folder)
    async with sessionmaker.begin() as session:
        cursor = await session.get(MailboxSync, (account.id, folder))
        if cursor is None or cursor.uidvalidity != state.uidvalidity:
            # Первое подключение или сброс UID на сервере: архив не разбираем,
            # начинаем с текущего момента
            await _reset_cursor(session, account.id, folder, state)
            log.info("account %s %s: baseline uid %s", account.id, folder, state.uidnext - 1)
            return 0
        last_uid = cursor.last_uid

    # UID n:* всегда включает последнее письмо, даже если n больше его UID
    found = await asyncio.to_thread(client.uids_after, last_uid)
    new_uids = sorted(uid for uid in found if uid > last_uid)

    saved = 0
    for batch in _batches(new_uids, FETCH_BATCH):
        raw_by_uid = await asyncio.to_thread(client.fetch_raw, batch)
        async with sessionmaker.begin() as session:
            for uid in batch:
                raw = raw_by_uid.get(uid)  # None — письмо удалили между SEARCH и FETCH
                if raw is not None and await save_message(
                    session, account, folder, uid, raw, attachments_dir
                ):
                    saved += 1
            await session.execute(
                update(MailboxSync)
                .where(MailboxSync.account_id == account.id, MailboxSync.folder == folder)
                .values(last_uid=batch[-1])
            )
    return saved


async def save_message(
    session: AsyncSession,
    account: AccountRef,
    folder: str,
    uid: int,
    raw: bytes,
    attachments_dir: Path,
    *,
    is_test: bool = False,
    enqueue_processing: bool = True,
) -> bool:
    """Сохраняет письмо, тред и задачу на обработку в одной транзакции. False — дубль в ящике."""
    parsed = parse_email(
        raw, fallback_message_id=f"<{account.id}.{uid}.{folder}@mailpulse.invalid>"
    )
    own_address = account.email.casefold()
    outgoing = parsed.from_addr == own_address
    stmt = (
        insert(Message)
        .values(
            account_id=account.id,
            folder=folder,
            uid=uid,
            message_id_hdr=parsed.message_id[:998],
            in_reply_to=parsed.in_reply_to[:998] if parsed.in_reply_to else None,
            references=parsed.references,
            from_addr=parsed.from_addr[:320],
            from_name=parsed.from_name[:255] if parsed.from_name else None,
            to_addrs=parsed.to,
            cc_addrs=parsed.cc,
            subject=parsed.subject,
            sent_at=parsed.sent_at,
            body_text=parsed.body_text,
            headers=parsed.headers,
            has_attachments=bool(parsed.attachments),
            outgoing=outgoing,
            is_test=is_test,
        )
        .on_conflict_do_nothing(index_elements=[Message.account_id, Message.message_id_hdr])
        .returning(Message.id)
    )
    message_id = (await session.execute(stmt)).scalar_one_or_none()
    if message_id is None:
        return False

    sent_at = parsed.sent_at or datetime.now(UTC)
    # То же письмо уже пришло в другой ящик пользователя (например, копия на оба адреса)
    copy = await _copy_in_other_account(session, account, parsed.message_id, message_id)
    if copy is not None and copy.thread_id is not None:
        thread_id = copy.thread_id
    else:
        thread_id = await resolve_thread(
            session,
            user_id=account.user_id,
            subject=parsed.subject,
            in_reply_to=parsed.in_reply_to,
            references=parsed.references,
            sent_at=sent_at,
        )
    await session.execute(
        update(Message).where(Message.id == message_id).values(thread_id=thread_id)
    )

    for attachment in parsed.attachments:
        digest = hashlib.sha256(attachment.payload).hexdigest()
        path = await asyncio.to_thread(_store_blob, attachments_dir, digest, attachment.payload)
        session.add(
            Attachment(
                message_id=message_id,
                filename=attachment.filename[:512],
                mime=attachment.mime[:255],
                size=attachment.size,
                sha256=digest,
                storage_path=str(path),
            )
        )

    if copy is not None:
        return True  # копия уже учтена в истории и разобрана — вторая карточка не нужна

    if is_test:
        # Тестовое письмо разбираем и шлём карточку, но архив и историю не трогаем
        await queue.enqueue(session, "process_email", {"message_id": message_id})
        return True

    await record_contact(
        session,
        user_id=account.user_id,
        own_address=own_address,
        sender=parsed.from_addr,
        recipients=[a["address"] for a in (*parsed.to, *parsed.cc)],
        outgoing=outgoing,
        is_reply=bool(parsed.in_reply_to) or is_reply_subject(parsed.subject),
        at=sent_at,
    )
    # enqueue_processing=False у импорта архива: письма прошлого индексируем и учитываем
    # в истории, но не классифицируем и не шлём по ним карточки
    if not outgoing and enqueue_processing:
        await queue.enqueue(session, "process_email", {"message_id": message_id})
    # Индексируются и исходящие: ответы пользователя — полезный контекст для похожих писем
    await queue.enqueue(session, "index_email", {"message_id": message_id})
    return True


async def _copy_in_other_account(
    session: AsyncSession, account: AccountRef, message_id_hdr: str, new_id: int
):
    stmt = (
        select(Message.id, Message.thread_id)
        .join(MailAccount, MailAccount.id == Message.account_id)
        .where(
            MailAccount.user_id == account.user_id,
            Message.message_id_hdr == message_id_hdr[:998],
            Message.id != new_id,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).one_or_none()


async def _reset_cursor(
    session: AsyncSession, account_id: int, folder: str, state: FolderState
) -> None:
    stmt = insert(MailboxSync).values(
        account_id=account_id,
        folder=folder,
        uidvalidity=state.uidvalidity,
        last_uid=max(state.uidnext - 1, 0),
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[MailboxSync.account_id, MailboxSync.folder],
            set_={"uidvalidity": stmt.excluded.uidvalidity, "last_uid": stmt.excluded.last_uid},
        )
    )


def _store_blob(root: Path, digest: str, payload: bytes) -> Path:
    """Вложения хранятся по хэшу содержимого: одинаковые файлы из разных писем — один файл."""
    path = root / digest[:2] / digest
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
    return path


def _batches(items: list[int], size: int) -> Iterator[list[int]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
