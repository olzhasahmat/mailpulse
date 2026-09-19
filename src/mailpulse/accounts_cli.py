"""Подключение почтовых ящиков из терминала, пока нет Mini App.

uv run mailpulse-accounts add you@gmail.com
uv run mailpulse-accounts list
uv run mailpulse-accounts disable you@gmail.com
"""

import argparse
import asyncio
import getpass
import sys

from imapclient import IMAPClient
from imapclient.exceptions import LoginError
from sqlalchemy import func, select

from mailpulse.config import get_settings
from mailpulse.db.models import AccountStatus, AuthType, MailAccount, Provider, User
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.mail import accounts
from mailpulse.security import SecretsCipher


async def _resolve_user_id(session, tg_user_id: int | None) -> int | str:
    if tg_user_id is not None:
        user_id = await session.scalar(select(User.id).where(User.tg_user_id == tg_user_id))
        return user_id if user_id is not None else f"пользователь Telegram {tg_user_id} не найден"
    ids = list((await session.execute(select(User.id).limit(2))).scalars())
    if not ids:
        return "пользователей нет: сначала отправь боту /start"
    if len(ids) > 1:
        return "пользователей несколько: укажи --tg-user-id"
    return ids[0]


async def _save(email: str, tg_user_id: int | None, preset, secret_enc: bytes) -> int | str:
    engine = make_engine()
    try:
        async with make_sessionmaker(engine).begin() as session:
            user_id = await _resolve_user_id(session, tg_user_id)
            if isinstance(user_id, str):
                return user_id
            return await accounts.upsert_account(
                session,
                user_id=user_id,
                email=email,
                preset=preset,
                auth_type=AuthType.APP_PASSWORD,
                secret_enc=secret_enc,
            )
    finally:
        await engine.dispose()


def cmd_add(args: argparse.Namespace) -> int:
    try:
        cipher = SecretsCipher.from_settings(get_settings())
    except RuntimeError as exc:
        print(f"✗ {exc}")
        return 2

    provider = Provider(args.provider) if args.provider else None
    preset = accounts.preset_for(args.email, provider)
    if preset is None:
        print("✗ Неизвестный почтовый сервис. Для Gmail на своём домене укажи --provider gmail")
        return 2
    if preset.provider is Provider.MICROSOFT:
        print("✗ Microsoft 365 подключается через OAuth2 — появится на день 4")
        return 2

    password = getpass.getpass(f"Пароль приложения для {args.email}: ")
    if preset.provider is Provider.GMAIL:
        password = password.replace(" ", "")  # Google показывает пароль приложения группами

    try:
        with IMAPClient(preset.imap_host, port=preset.imap_port, ssl=True, timeout=30) as client:
            client.login(args.email, password)
    except LoginError as exc:
        print(f"✗ Вход не удался: {exc}")
        return 1

    result = asyncio.run(_save(args.email, args.tg_user_id, preset, cipher.encrypt(password)))
    if isinstance(result, str):
        print(f"✗ {result}")
        return 1
    print(f"✓ Ящик {args.email} подключён (id {result}).")
    print("  Сервис ingest подхватит его в течение 30 секунд; уведомления придут на новые письма.")
    return 0


async def _list() -> list[tuple]:
    engine = make_engine()
    try:
        async with make_sessionmaker(engine)() as session:
            stmt = select(
                MailAccount.id,
                MailAccount.email,
                MailAccount.provider,
                MailAccount.status,
                MailAccount.last_error,
            ).order_by(MailAccount.id)
            return list(await session.execute(stmt))
    finally:
        await engine.dispose()


def cmd_list(args: argparse.Namespace) -> int:
    rows = asyncio.run(_list())
    if not rows:
        print("Ящиков нет: mailpulse-accounts add you@gmail.com")
    for account_id, email, provider, status, error in rows:
        suffix = f" — {error}" if error else ""
        print(f"{account_id:>3}  {email:<35} {provider.value:<10} {status.value}{suffix}")
    return 0


async def _disable(email: str) -> int:
    engine = make_engine()
    try:
        async with make_sessionmaker(engine).begin() as session:
            stmt = select(MailAccount.id).where(func.lower(MailAccount.email) == email.casefold())
            ids = list((await session.execute(stmt)).scalars())
            for account_id in ids:
                await accounts.set_status(session, account_id, AccountStatus.DISABLED)
            return len(ids)
    finally:
        await engine.dispose()


def cmd_disable(args: argparse.Namespace) -> int:
    count = asyncio.run(_disable(args.email))
    print(f"✓ Отключено ящиков: {count}" if count else f"✗ Ящик {args.email} не найден")
    return 0 if count else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Почтовые ящики MailPulse")
    commands = parser.add_subparsers(dest="command", required=True)

    add = commands.add_parser("add", help="подключить ящик по паролю приложения")
    add.add_argument("email")
    add.add_argument("--provider", choices=[Provider.GMAIL.value], help="для Gmail на своём домене")
    add.add_argument("--tg-user-id", type=int, help="если пользователей несколько")
    add.set_defaults(handler=cmd_add)

    commands.add_parser("list", help="список ящиков").set_defaults(handler=cmd_list)

    disable = commands.add_parser("disable", help="перестать слушать ящик")
    disable.add_argument("email")
    disable.set_defaults(handler=cmd_disable)

    args = parser.parse_args()
    return args.handler(args)


def run() -> None:
    sys.exit(main())
