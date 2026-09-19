"""Применяет миграции Alembic и создаёт таблицы чекпоинтера LangGraph."""

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from mailpulse.config import get_settings
from mailpulse.observability import setup_logging

log = logging.getLogger(__name__)


async def setup_checkpointer(dsn: str) -> None:
    async with AsyncPostgresSaver.from_conn_string(dsn) as checkpointer:
        await checkpointer.setup()


def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    ini = Path.cwd() / "alembic.ini"
    if not ini.exists():
        raise SystemExit(f"{ini} не найден: запускай из корня репозитория")
    command.upgrade(Config(str(ini)), "head")
    asyncio.run(setup_checkpointer(settings.psycopg_dsn))
    log.info("migrations applied, checkpointer tables ready")
