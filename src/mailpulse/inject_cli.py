"""Подкладывает тестовое письмо в конвейер без IMAP: разбор, поиск, карточка — как у настоящего.

    make inject-email account=yandex template=urgent
    make inject-email account=both template=invoice   # копия в оба ящика — проверка дедупликации
    make inject-email account=gmail template=reply     # ответ в тред предыдущего invoice

Требует ANTHROPIC_API_KEY и TELEGRAM_BOT_TOKEN в .env, иначе воркер не разберёт письмо.
Тестовые письма помечаются is_test: в RAG-индекс, историю и статистику не попадают.
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime

from sqlalchemy import select

from mailpulse.config import get_settings
from mailpulse.db.models import MailAccount, Message, Provider, User
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.mail.sync import AccountRef, save_message
from mailpulse.testing_templates import (
    REPLY_TO_KEY,
    all_keys,
    build_invoice_image,
    build_raw,
    get_template,
)

ACCOUNT_CHOICES = {
    "gmail": Provider.GMAIL,
    "yandex": Provider.IMAP,
    "microsoft": Provider.MICROSOFT,
}


def _scan_attachment() -> tuple[str, str, bytes]:
    return ("schet_771.png", "image/png", build_invoice_image())


async def resolve_accounts(session, which: str) -> list[AccountRef]:
    stmt = select(MailAccount).join(User, User.id == MailAccount.user_id)
    accounts = list((await session.execute(stmt)).scalars())
    if which != "both":
        provider = ACCOUNT_CHOICES[which]
        accounts = [a for a in accounts if a.provider == provider]
    return [AccountRef(id=a.id, user_id=a.user_id, email=a.email) for a in accounts]


async def find_parent(session, account_ids: list[int], reply_to_key: str) -> str | None:
    """Message-ID последнего письма, для которого шаблон reply — продолжение треда."""
    subject = get_template(reply_to_key).subject
    stmt = (
        select(Message.message_id_hdr)
        .where(Message.account_id.in_(account_ids), Message.subject == subject)
        .order_by(Message.id.desc())
        .limit(1)
    )
    return await session.scalar(stmt)


async def inject(which: str, template_key: str) -> int:
    template = get_template(template_key)
    engine = make_engine()
    sessionmaker = make_sessionmaker(engine)
    settings = get_settings()
    try:
        async with sessionmaker() as session:
            accounts = await resolve_accounts(session, which)
            if not accounts:
                print(f"✗ Нет подключённых ящиков для '{which}'. Проверь mailpulse-accounts list")
                return 1
            in_reply_to = None
            if template_key == "reply":
                in_reply_to = await find_parent(session, [a.id for a in accounts], REPLY_TO_KEY)
                if in_reply_to is None:
                    print("✗ Нет письма для ответа. Сначала: make inject-email template=invoice")
                    return 1

        now = datetime.now(UTC)
        # Один Message-ID на все копии: письмо «в оба ящика» дедуплицируется как настоящее
        shared_id = None
        for account in accounts:
            uid = int(time.time() * 1000) % 2_000_000_000
            raw, shared_id = build_raw(
                template,
                to_addr=account.email,
                message_id=shared_id,
                in_reply_to=in_reply_to,
                now=now,
                attachment=_scan_attachment() if template_key == "scan" else None,
            )
            async with sessionmaker.begin() as session:
                saved = await save_message(
                    session, account, "INBOX", uid, raw, settings.attachments_dir, is_test=True
                )
            status = "подложено" if saved else "уже есть (дубль)"
            print(f"  {account.email}: {status}")
    finally:
        await engine.dispose()

    print(f"\n✓ Шаблон '{template_key}' ({template.description}).")
    print("  Воркер разберёт письмо; карточка придёт в Telegram с пометкой 🧪 [тест].")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--account", choices=[*ACCOUNT_CHOICES, "both"], default="both")
    parser.add_argument("--template", choices=all_keys(), required=True)
    args = parser.parse_args()
    return asyncio.run(inject(args.account, args.template))


def run() -> None:
    sys.exit(main())
