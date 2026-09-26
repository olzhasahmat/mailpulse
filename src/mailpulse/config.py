"""Настройки из переменных окружения и .env.

ANTHROPIC_API_KEY и LANGSMITH_* здесь нет: Anthropic SDK и LangSmith читают их из окружения сами.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Пустое значение в .env (`KEY=`) считается незаданным, а не пустой строкой
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    app_env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"

    database_url: str = "postgresql+psycopg://mailpulse:mailpulse@localhost:5433/mailpulse"

    telegram_bot_token: SecretStr | None = None
    # Публичный HTTPS-адрес Mini App для кнопки «Подключить почту» в /start
    miniapp_url: str | None = None
    secrets_key: SecretStr | None = None

    microsoft_client_id: str | None = None
    microsoft_tenant: str = "organizations"

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    mcp_host: str = "127.0.0.1"
    mcp_port: int = 8001
    # Чью почту отдаёт MCP-сервер. Не задан и пользователь один — берётся он
    mcp_user_id: int | None = None

    attachments_dir: Path = Path("data/attachments")
    embedding_model: str = "intfloat/multilingual-e5-large"
    embedding_cache_dir: Path = Path("data/models")

    worker_poll_interval_s: float = 2.0
    worker_stale_job_after_s: int = 600

    @property
    def psycopg_dsn(self) -> str:
        """DSN без драйвера SQLAlchemy — для psycopg и чекпоинтера LangGraph."""
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
