from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from mailpulse.config import get_settings
from mailpulse.db import models  # noqa: F401 — регистрирует таблицы в metadata
from mailpulse.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def database_url() -> str:
    # Тесты подставляют URL тестовой базы через sqlalchemy.url
    return config.get_main_option("sqlalchemy.url") or get_settings().database_url


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    # Таблицы чекпоинтера создаёт LangGraph (AsyncPostgresSaver.setup), Alembic их не трогает
    return not (type_ == "table" and reflected and compare_to is None)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
