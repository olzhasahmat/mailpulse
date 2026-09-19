"""Проверка Telegram initData: подпись доказывает, кто пользователь, без ввода Telegram-аккаунта.

Алгоритм из документации Telegram Web Apps: секрет = HMAC-SHA256("WebAppData", bot_token),
затем HMAC этим секретом по отсортированной строке полей должен совпасть с полем hash.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

DEFAULT_MAX_AGE_S = 24 * 3600


class InitDataError(ValueError):
    pass


@dataclass(frozen=True)
class TelegramUser:
    tg_user_id: int
    first_name: str
    username: str | None


def verify_init_data(
    init_data: str, bot_token: str, *, max_age_s: int = DEFAULT_MAX_AGE_S, now: float | None = None
) -> TelegramUser:
    """Проверяет подпись initData и возвращает пользователя. Иначе InitDataError."""
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = fields.pop("hash", None)
    if not received_hash:
        raise InitDataError("нет поля hash")

    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise InitDataError("подпись не совпадает")

    auth_date = int(fields.get("auth_date", "0"))
    current = now if now is not None else time.time()
    if max_age_s and current - auth_date > max_age_s:
        raise InitDataError("initData устарел")

    try:
        user = json.loads(fields["user"])
    except (KeyError, json.JSONDecodeError) as exc:
        raise InitDataError("нет данных пользователя") from exc
    return TelegramUser(
        tg_user_id=int(user["id"]),
        first_name=user.get("first_name", ""),
        username=user.get("username"),
    )


def build_init_data(bot_token: str, user: dict, *, auth_date: int) -> str:
    """Собирает подписанный initData — для тестов и локальной разработки."""
    fields = {"user": json.dumps(user, separators=(",", ":")), "auth_date": str(auth_date)}
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{key}={value}" for key, value in fields.items())
