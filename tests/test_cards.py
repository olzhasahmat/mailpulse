import pytest

from mailpulse.bot.cards import (
    callback_data,
    parse_callback_data,
    render_card,
    render_quarantine,
)
from tests.fakes import make_email


def test_callback_data_roundtrip():
    assert parse_callback_data(callback_data(42, "not_important")) == (42, "not_important")


@pytest.mark.parametrize("data", ["mp:abc:read", "xx:1:read", "mp:1:launch", "mp:1", ""])
def test_malformed_callback_data_is_rejected(data):
    assert parse_callback_data(data) is None


def test_card_escapes_html_and_formats_extracted_fields():
    email = make_email(from_name=None, subject="<script>Счёт</script>")
    triage = {
        "reasoning": "",
        "importance": 3,
        "category": "finance",
        "needs_reply": False,
        "suspicious": False,
        "extracted": {},
    }
    summary = {
        "tldr": "Оплатить счёт №245 до 15 сентября.",
        "amounts": [{"value": 245000.0, "currency": "KZT"}],
        "deadlines": ["2026-09-15"],
        "action_items": ["Оплатить счёт"],
    }

    card = render_card(email, triage, summary)

    assert "&lt;script&gt;" in card and "<script>" not in card
    assert "🔴 Срочно" in card
    assert "245 000 KZT" in card
    assert "2026-09-15" in card
    assert "• Оплатить счёт" in card


def test_quarantine_lists_reasons():
    text = render_quarantine(make_email(), {"suspicious": True, "reasons": ["инструкция для ИИ"]})

    assert "Подозрительное письмо" in text
    assert "• инструкция для ИИ" in text


def test_card_shows_sender_name_and_address():
    email = make_email(from_name="Дана <Ментор>", from_addr="dana@mail.ru")
    triage = {
        "reasoning": "",
        "importance": 2,
        "category": "work",
        "needs_reply": True,
        "suspicious": False,
        "extracted": {},
    }
    summary = {"tldr": "Нужна ссылка на демо", "amounts": [], "deadlines": [], "action_items": []}

    card = render_card(email, triage, summary)

    assert "От: <b>Дана &lt;Ментор&gt;</b> (dana@mail.ru)" in card


def test_card_without_sender_name_shows_address_only():
    email = make_email(from_name=None, from_addr="no-reply@bank.kz")
    triage = {
        "reasoning": "",
        "importance": 3,
        "category": "finance",
        "needs_reply": False,
        "suspicious": False,
        "extracted": {},
    }
    summary = {"tldr": "Списание", "amounts": [], "deadlines": [], "action_items": []}

    assert "От: no-reply@bank.kz\n" in render_card(email, triage, summary)


def test_quarantine_card_marks_test_email():
    from mailpulse.bot.cards import render_quarantine

    email = make_email(is_test=True, from_addr="phish@evil.example")
    text = render_quarantine(email, {"suspicious": True, "reasons": ["инструкция для ИИ"]})

    assert text.startswith("🧪 [тест] ⚠️")


def test_card_buttons_include_reply_only_when_needed():
    from mailpulse.bot.cards import card_buttons

    with_reply = [action for action, _ in card_buttons(needs_reply=True)]
    without = [action for action, _ in card_buttons(needs_reply=False)]

    assert with_reply == ["draft", "read", "not_important"]
    assert without == ["read", "not_important"]
