"""Проверка ключей из .env: каждый сервис отвечает на безопасный запрос. Значения не выводятся.

make check-keys
"""

import asyncio
import os
import sys

from cryptography.fernet import Fernet
from sqlalchemy import func, select

from mailpulse.config import get_settings

LANGSMITH_HINTS = {
    "401": "ключ неверный или удалён — создай новый в Settings → API Keys",
    "403": (
        "ключ не подходит этому серверу: для EU-аккаунта нужен "
        "LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com, "
        "для ключа организации — LANGSMITH_WORKSPACE_ID"
    ),
}


def check_secrets_key() -> bool:
    key = get_settings().secrets_key
    if key is None:
        print("✗ SECRETS_KEY не задан: make secret-key и вставь значение в .env")
        return False
    try:
        Fernet(key.get_secret_value().encode())
    except ValueError:
        print("✗ SECRETS_KEY неверного формата: сгенерируй заново через make secret-key")
        return False
    print("✓ SECRETS_KEY")
    return True


async def check_anthropic() -> bool:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("✗ ANTHROPIC_API_KEY не задан")
        return False
    from anthropic import AsyncAnthropic, NotFoundError

    needed = ["claude-haiku-4-5", "claude-sonnet-5"]
    try:
        async with AsyncAnthropic() as client:
            # Список моделей отдаёт датированные ID (claude-haiku-4-5-20251001), поэтому
            # проверяем алиасы, которые использует приложение, через retrieve
            missing = []
            for model in needed:
                try:
                    await client.models.retrieve(model)
                except NotFoundError:
                    missing.append(model)
    except Exception as exc:
        print(f"✗ ANTHROPIC_API_KEY не принят: {type(exc).__name__}")
        return False
    if missing:
        print(f"✗ ANTHROPIC_API_KEY принят, но нет доступа к моделям: {', '.join(missing)}")
        return False
    print("✓ ANTHROPIC_API_KEY, модели доступны")
    return True


def check_langsmith() -> bool:
    key = os.environ.get("LANGSMITH_API_KEY", "")
    if not key:
        print("✗ LANGSMITH_API_KEY не задан (без него не будет трейсов)")
        return False
    if not key.startswith("lsv2_"):
        # LangSmith отвечает на такой ключ 403 Forbidden, а не «неверный токен»
        print("✗ LANGSMITH_API_KEY должен начинаться со строчных lsv2_ — ключ искажён при вставке")
        return False
    from langsmith import Client

    try:
        list(Client().list_projects(limit=1))
    except Exception as exc:
        # Текст ошибки LangSmith содержит код ответа и URL, но не сам ключ
        status = next((code for code in ("401", "403", "404") if code in str(exc)), "?")
        print(f"✗ LANGSMITH_API_KEY не принят: HTTP {status}, {LANGSMITH_HINTS.get(status, '')}")
        return False
    tracing = os.environ.get("LANGSMITH_TRACING", "").lower() == "true"
    warning = "" if tracing else " — но LANGSMITH_TRACING не true, трейсов не будет"
    print(f"✓ LANGSMITH_API_KEY{warning}")
    return True


async def check_telegram() -> bool:
    token = get_settings().telegram_bot_token
    if token is None:
        print("✗ TELEGRAM_BOT_TOKEN не задан")
        return False
    from mailpulse.bot.notifier import create_bot

    try:
        bot = create_bot(token.get_secret_value())
        try:
            me = await bot.get_me()
        finally:
            await bot.session.close()
    except Exception as exc:
        print(f"✗ TELEGRAM_BOT_TOKEN не принят: {type(exc).__name__}")
        return False
    print(f"✓ TELEGRAM_BOT_TOKEN, бот @{me.username}")
    return True


async def check_users() -> None:
    from mailpulse.db.models import MailAccount, User
    from mailpulse.db.session import make_engine, make_sessionmaker

    engine = make_engine()
    try:
        async with make_sessionmaker(engine)() as session:
            users = await session.scalar(select(func.count()).select_from(User))
            accounts = await session.scalar(select(func.count()).select_from(MailAccount))
    except Exception as exc:
        print(f"  База недоступна ({type(exc).__name__}): make db")
        return
    finally:
        await engine.dispose()
    print(f"  Пользователей: {users}" + ("" if users else " — отправь боту /start"))
    print(f"  Ящиков: {accounts}" + ("" if accounts else " — make account-add email=you@gmail.com"))


async def main() -> int:
    results = [
        check_secrets_key(),
        await check_anthropic(),
        check_langsmith(),
        await check_telegram(),
    ]
    await check_users()
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
