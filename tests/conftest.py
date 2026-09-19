import os
from collections.abc import AsyncIterator

import psycopg
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.config import get_settings
from mailpulse.db.base import Base
from mailpulse.db.provision import ensure_database
from mailpulse.db.session import make_engine, make_sessionmaker

# Тесты не шлют трейсы в LangSmith, даже если трейсинг включён в .env
os.environ["LANGSMITH_TRACING"] = "false"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """Отдельная база <имя>_test с применёнными миграциями. Без Postgres тесты с БД пропускаются."""
    try:
        return ensure_database(get_settings().database_url, "test")
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres недоступен, запусти `make db`: {exc}")


@pytest.fixture
async def db(test_database_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Сессии к тестовой базе; все таблицы приложения очищаются перед тестом."""
    engine = make_engine(test_database_url)
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield make_sessionmaker(engine)
    await engine.dispose()
