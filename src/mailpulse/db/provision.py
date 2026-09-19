"""Отдельные базы для тестов и оценок: создаются рядом с основной и мигрируются до head."""

from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[3]


def ensure_database(base_url: str, suffix: str) -> str:
    """URL базы «<имя>_<suffix>». Если Postgres недоступен — psycopg.OperationalError."""
    url = make_url(base_url)
    target = url.set(database=f"{url.database}_{suffix}")
    admin_dsn = url.set(drivername="postgresql").render_as_string(hide_password=False)
    with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=3) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (target.database,)
        ).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target.database)))

    rendered = target.render_as_string(hide_password=False)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", rendered)
    command.upgrade(config, "head")
    return rendered
