"""Тексты сообщений и данные кнопок. Без сети, поэтому тестируется отдельно от бота."""

from html import escape
from typing import Any

from mailpulse.agent.state import EmailView, GuardrailVerdict, Summary, TriageResult

CALLBACK_PREFIX = "mp"
# «✍️ Ответить» появится вместе с черновиками на день 7
BASE_CARD_BUTTONS = (("read", "✅ Прочитано"), ("not_important", "🔕 Не важно"))
REPLY_BUTTON = ("draft", "✍️ Ответить")


def card_buttons(needs_reply: bool) -> tuple[tuple[str, str], ...]:
    return (REPLY_BUTTON, *BASE_CARD_BUTTONS) if needs_reply else BASE_CARD_BUTTONS


ACTION_RESULTS = {
    "read": "✅ Отмечено прочитанным",
    "not_important": "🔕 Учту: такие письма не важны",
    "draft": "✍️ Готовлю черновик…",
    "send": "📤 Отправляю…",
    "edit": "✏️ Жду правки",
    "cancel": "Отменено",
}
IMPORTANCE_LABELS = {3: "🔴 Срочно", 2: "🟡 Важно", 1: "⚪️ К сведению", 0: "Шум"}
MAX_TLDR_CHARS = 1500
MAX_ACTION_ITEMS = 5


def callback_data(message_id: int, action: str) -> str:
    return f"{CALLBACK_PREFIX}:{message_id}:{action}"


def parse_callback_data(data: str) -> tuple[int, str] | None:
    prefix, _, rest = data.partition(":")
    message_id, _, action = rest.partition(":")
    if prefix != CALLBACK_PREFIX or not message_id.isdigit() or action not in ACTION_RESULTS:
        return None
    return int(message_id), action


def render_card(email: EmailView, triage: TriageResult, summary: Summary) -> str:
    label = IMPORTANCE_LABELS.get(triage["importance"], "")
    if email.get("is_test"):
        label = f"🧪 [тест] {label}"
    lines = [
        f"<b>{label}</b> · {escape(triage['category'])}",
        f"От: {format_sender(email)}",
        f"Тема: {escape(email['subject'] or '(без темы)')}",
        "",
        escape(summary["tldr"][:MAX_TLDR_CHARS]),
    ]
    if summary["deadlines"]:
        lines.append("📅 " + escape(", ".join(summary["deadlines"])))
    if summary["amounts"]:
        lines.append("💰 " + escape(", ".join(_format_amount(a) for a in summary["amounts"])))
    lines.extend(f"• {escape(item)}" for item in summary["action_items"][:MAX_ACTION_ITEMS])
    return "\n".join(lines)


def format_sender(email: EmailView) -> str:
    """Имя жирным и адрес: по имени легко спутать отправителя, адрес видно всегда."""
    address = escape(email["from_addr"])
    if not email["from_name"]:
        return address
    return f"<b>{escape(email['from_name'])}</b> ({address})"


def render_quarantine(email: EmailView, verdict: GuardrailVerdict) -> str:
    reasons = "\n".join(f"• {escape(reason)}" for reason in verdict["reasons"])
    prefix = "🧪 [тест] " if email.get("is_test") else ""
    return (
        f"{prefix}⚠️ <b>Подозрительное письмо</b>\n"
        f"От: {escape(email['from_addr'])}\n"
        f"Тема: {escape(email['subject'] or '(без темы)')}\n\n"
        f"{reasons}\n\n"
        "Не переходите по ссылкам из письма и не отвечайте на него."
    )


def render_draft(body: str) -> str:
    return f"✍️ <b>Черновик ответа</b>\n\n{escape(body)}"


def _format_amount(amount: dict[str, Any]) -> str:
    value = float(amount.get("value", 0))
    number = f"{value:,.2f}".rstrip("0").rstrip(".").replace(",", " ")
    return f"{number} {amount.get('currency', '')}".strip()
