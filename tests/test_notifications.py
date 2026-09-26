from mailpulse.notifications import account_deleted_message


def test_message_has_service_revoke_link():
    text = account_deleted_message("olzhas@gmail.com")

    assert "olzhas@gmail.com" in text
    assert "Важно" in text
    assert "myaccount.google.com/apppasswords" in text
    assert "Gmail" in text


def test_yandex_link():
    text = account_deleted_message("you@yandex.ru")

    assert "id.yandex.ru/security/app-passwords" in text
    assert "Яндекс" in text


def test_unknown_domain_falls_back_to_generic_advice():
    text = account_deleted_message("me@corp.example")

    assert "настройках безопасности" in text
    assert "http" not in text  # нет конкретной ссылки — общий совет
