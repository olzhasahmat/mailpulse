"""Запись ключей в .env. Ввод скрыт и не попадает в историю shell.

make set-keys
"""

import getpass
import shutil
import sys
from pathlib import Path

ENV_PATH = Path(".env")
KEYS = (
    ("ANTHROPIC_API_KEY", "Anthropic API key (sk-ant-…)"),
    ("LANGSMITH_API_KEY", "LangSmith API key (lsv2_…)"),
    ("TELEGRAM_BOT_TOKEN", "Токен Telegram-бота (123456789:AA…)"),
)


def set_values(text: str, values: dict[str, str]) -> str:
    """Меняет значения существующих строк KEY=..., отсутствующие ключи дописывает в конец."""
    lines = text.splitlines()
    replaced = set()
    for index, line in enumerate(lines):
        name, sep, _ = line.partition("=")
        if sep and name.strip() in values:
            lines[index] = f"{name.strip()}={values[name.strip()]}"
            replaced.add(name.strip())
    lines += [f"{name}={value}" for name, value in values.items() if name not in replaced]
    return "\n".join(lines) + "\n"


def main() -> int:
    if not ENV_PATH.exists():
        shutil.copy(".env.example", ENV_PATH)

    values = {}
    for name, prompt in KEYS:
        value = getpass.getpass(f"{prompt}, Enter — оставить как есть: ").strip()
        if value:
            values[name] = value
    if not values:
        print("Ничего не изменено")
        return 0
    if "LANGSMITH_API_KEY" in values:
        values["LANGSMITH_TRACING"] = "true"

    ENV_PATH.write_text(set_values(ENV_PATH.read_text(), values))
    ENV_PATH.chmod(0o600)
    print(f"✓ Записано в .env: {', '.join(values)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
