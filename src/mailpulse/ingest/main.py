"""Приём почты: IMAP IDLE на каждый активный ящик, новые письма — в БД и очередь.

Работает одним экземпляром: копии сервиса дублировали бы соединения с ящиками
(DECISIONS.md, ADR-015).
"""

import asyncio
import contextlib
import logging
import signal
import threading
from pathlib import Path

from imapclient.exceptions import LoginError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.config import get_settings
from mailpulse.db.models import AccountStatus
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.mail import accounts
from mailpulse.mail.imap import ImapMailbox
from mailpulse.mail.sync import sync_folder
from mailpulse.observability import setup_logging
from mailpulse.security import SecretsCipher

log = logging.getLogger(__name__)

INBOX = "INBOX"
ACCOUNTS_REFRESH_S = 30
IDLE_MAX_WAIT_S = 9 * 60
BACKOFF_START_S = 5
BACKOFF_MAX_S = 300


async def run_account(
    account_id: int,
    sessionmaker: async_sessionmaker[AsyncSession],
    cipher: SecretsCipher,
    attachments_dir: Path,
    stop: threading.Event,
) -> None:
    backoff = BACKOFF_START_S
    while not stop.is_set():
        async with sessionmaker() as session:
            loaded = await accounts.load_credentials(session, cipher, account_id)
        if loaded is None:
            return
        ref, creds = loaded

        mailbox: ImapMailbox | None = None
        try:
            mailbox = await asyncio.to_thread(ImapMailbox.connect, creds, stop)
            sent_folder = await asyncio.to_thread(mailbox.sent_folder)
            log.info("account %s (%s): connected, sent: %s", account_id, creds.email, sent_folder)
            # «Отправленные» первыми: IDLE ждёт изменений в последней выбранной папке — INBOX
            folders = [sent_folder, INBOX] if sent_folder else [INBOX]
            backoff = BACKOFF_START_S
            while not stop.is_set():
                for folder in folders:
                    saved = await sync_folder(mailbox, sessionmaker, ref, attachments_dir, folder)
                    if saved:
                        log.info("account %s %s: %d new messages", account_id, folder, saved)
                await asyncio.to_thread(mailbox.wait_for_changes, IDLE_MAX_WAIT_S)
        except LoginError as exc:
            log.error("account %s: login failed, pausing account: %s", account_id, exc)
            async with sessionmaker.begin() as session:
                await accounts.set_status(session, account_id, AccountStatus.AUTH_FAILED, str(exc))
            return
        except Exception:
            log.exception("account %s: connection error, retry in %ss", account_id, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, BACKOFF_MAX_S)
        finally:
            if mailbox is not None:
                await asyncio.to_thread(mailbox.close)


async def run_ingest(stop_requested: asyncio.Event) -> None:
    settings = get_settings()
    cipher = SecretsCipher.from_settings(settings)
    engine = make_engine()
    sessionmaker = make_sessionmaker(engine)
    stop = threading.Event()
    tasks: dict[int, asyncio.Task[None]] = {}
    log.info("ingest started")
    try:
        while not stop_requested.is_set():
            async with sessionmaker() as session:
                active = await accounts.active_account_ids(session)

            for account_id, task in list(tasks.items()):
                if task.done() and not task.cancelled() and task.exception():
                    log.error("account %s: listener crashed: %r", account_id, task.exception())
                if task.done() or account_id not in active:
                    tasks.pop(account_id).cancel()
            for account_id in active - tasks.keys():
                tasks[account_id] = asyncio.create_task(
                    run_account(account_id, sessionmaker, cipher, settings.attachments_dir, stop)
                )

            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_requested.wait(), timeout=ACCOUNTS_REFRESH_S)
    finally:
        stop.set()
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await engine.dispose()
        log.info("ingest stopped")


def run() -> None:
    setup_logging(get_settings().log_level)

    async def main() -> None:
        stop_requested = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_requested.set)
        await run_ingest(stop_requested)

    asyncio.run(main())
