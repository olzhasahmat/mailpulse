"""Импорт архива почты в RAG-индекс и историю. Классификацию и карточки не запускает.

    make import-archive                 # оба ящика, последние 90 дней
    make import-archive days=30         # только последний месяц
    make import-archive account=yandex  # один ящик

Индексацию выполняет воркер в фоне (задачи index_email), поэтому команда завершается быстро,
а эмбеддинги считаются постепенно. Требует SECRETS_KEY; ключи LLM не нужны.
"""

import argparse
import asyncio
import logging
import sys
import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from mailpulse.config import get_settings
from mailpulse.db.models import AccountStatus, MailAccount, Provider, User
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.mail import accounts
from mailpulse.mail.archive import ImportStats, import_folder
from mailpulse.mail.imap import ImapMailbox
from mailpulse.observability import setup_logging
from mailpulse.security import SecretsCipher

log = logging.getLogger(__name__)

ACCOUNT_PROVIDERS = {
    "gmail": Provider.GMAIL,
    "yandex": Provider.IMAP,
    "microsoft": Provider.MICROSOFT,
}


async def account_ids(which: str) -> list[int]:
    engine = make_engine()
    try:
        async with make_sessionmaker(engine)() as session:
            stmt = (
                select(MailAccount.id).join(User).where(MailAccount.status == AccountStatus.ACTIVE)
            )
            if which != "all":
                stmt = stmt.where(MailAccount.provider == ACCOUNT_PROVIDERS[which])
            return list((await session.execute(stmt)).scalars())
    finally:
        await engine.dispose()


async def import_account(account_id: int, since, cipher, settings, stop) -> ImportStats:
    engine = make_engine()
    sessionmaker = make_sessionmaker(engine)
    mailbox = None
    total = ImportStats()
    try:
        async with sessionmaker() as session:
            loaded = await accounts.load_credentials(session, cipher, account_id)
        if loaded is None:
            return total
        ref, creds = loaded

        mailbox = await asyncio.to_thread(ImapMailbox.connect, creds, stop)
        sent = await asyncio.to_thread(mailbox.sent_folder)
        folders = ["INBOX", *([sent] if sent else [])]
        print(f"  {creds.email}: папки {folders}")
        for folder in folders:
            stats = await import_folder(
                mailbox, sessionmaker, ref, settings.attachments_dir, folder, since
            )
            print(
                f"    {folder}: найдено {stats.seen}, сохранено {stats.saved}, "
                f"пропущено {stats.skipped_existing}"
            )
            total = total.merge(stats)
    except Exception:
        log.exception("account %s: import failed", account_id)
    finally:
        if mailbox is not None:
            await asyncio.to_thread(mailbox.close)
        await engine.dispose()
    return total


async def run_import(which: str, days: int) -> int:
    settings = get_settings()
    try:
        cipher = SecretsCipher.from_settings(settings)
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 2

    ids = await account_ids(which)
    if not ids:
        print(f"✗ Нет активных ящиков для '{which}'. Проверь mailpulse-accounts list")
        return 1

    since = (datetime.now(UTC) - timedelta(days=days)).date()
    print(f"Импорт архива с {since.isoformat()} ({days} дней), ящиков: {len(ids)}")
    stop = threading.Event()
    total = ImportStats()
    for account_id in ids:
        total = total.merge(await import_account(account_id, since, cipher, settings, stop))

    print(
        f"\n✓ Сохранено {total.saved} писем (из {total.seen} найденных, "
        f"{total.skipped_existing} уже были). Воркер индексирует их в фоне: следи за "
        "`docker compose logs -f worker`."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--account", choices=[*ACCOUNT_PROVIDERS, "all"], default="all")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    setup_logging(get_settings().log_level)
    return asyncio.run(run_import(args.account, args.days))


def run() -> None:
    sys.exit(main())
