from mailpulse.config import Settings


def test_empty_values_count_as_missing(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("SECRETS_KEY", "")

    settings = Settings()

    assert settings.telegram_bot_token is None
    assert settings.secrets_key is None


def test_psycopg_dsn_drops_sqlalchemy_driver():
    settings = Settings(database_url="postgresql+psycopg://u:p@localhost:5433/db")

    assert settings.psycopg_dsn == "postgresql://u:p@localhost:5433/db"
