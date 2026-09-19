import time

import pytest

from mailpulse.api.auth import InitDataError, build_init_data, verify_init_data

TOKEN = "123456:test-bot-token"
USER = {"id": 42, "first_name": "Olzhas", "username": "olzhas"}


def test_valid_init_data_roundtrip():
    init_data = build_init_data(TOKEN, USER, auth_date=int(time.time()))

    user = verify_init_data(init_data, TOKEN)

    assert user.tg_user_id == 42
    assert user.username == "olzhas"


def test_wrong_token_is_rejected():
    init_data = build_init_data(TOKEN, USER, auth_date=int(time.time()))

    with pytest.raises(InitDataError, match="подпись"):
        verify_init_data(init_data, "999:other-token")


def test_tampered_user_is_rejected():
    init_data = build_init_data(TOKEN, USER, auth_date=int(time.time()))
    tampered = init_data.replace("Olzhas", "Hacker")

    with pytest.raises(InitDataError):
        verify_init_data(tampered, TOKEN)


def test_expired_init_data_is_rejected():
    init_data = build_init_data(TOKEN, USER, auth_date=1000)

    with pytest.raises(InitDataError, match="устарел"):
        verify_init_data(init_data, TOKEN, max_age_s=3600, now=1_000_000)


def test_missing_hash_is_rejected():
    with pytest.raises(InitDataError, match="hash"):
        verify_init_data("user=%7B%7D&auth_date=1", TOKEN)
