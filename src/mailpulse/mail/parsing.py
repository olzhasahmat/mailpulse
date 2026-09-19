"""Разбор писем RFC 822: заголовки, текст без HTML и цитат, вложения."""

import email
import email.policy
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime

from bs4 import BeautifulSoup, Tag

MAX_BODY_CHARS = 50_000
MAX_HIDDEN_CHARS = 2_000
KEPT_HEADERS = ("list-unsubscribe", "list-id", "precedence", "auto-submitted", "reply-to")
# Скрытый текст из HTML: вырезается из тела, но сохраняется для проверки на prompt injection.
# Сам по себе не подозрителен — так устроены прехедеры рассылок
HIDDEN_TEXT_HEADER = "x-mailpulse-hidden-text"

BLOCK_TAGS = ("p", "div", "tr", "li", "ul", "ol", "table", "h1", "h2", "h3", "h4", "h5", "h6")
QUOTE_SELECTORS = ("div.gmail_quote", "blockquote", "div.yahoo_quoted", "div.moz-cite-prefix")
# Outlook ставит шапку цитаты отдельным блоком, а сама цитата идёт следующими соседями
QUOTE_START_SELECTORS = ("div#divRplyFwdMsg", "div#appendonsend")
HIDDEN_STYLE_RE = re.compile(
    r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?![.\d])", re.I
)

FORWARD_RE = re.compile(
    r"^-{2,}\s*(Forwarded message|Пересылаемое сообщение|Переслано)\s*-{2,}", re.M | re.I
)
REPLY_HEADER_RES = (
    re.compile(r"^On .{5,300} wrote:\s*$", re.M),
    re.compile(r"^.{0,60}\d{4}\s*г\.\s*в\s*\d{1,2}:\d{2}.{0,300}:\s*$", re.M),
    re.compile(r"^.{3,300}\s(пишет|написал|написала|написал\(а\)):\s*$", re.M),
    re.compile(r"^-{2,}\s*(Original Message|Исходное сообщение)\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^(From|От):\s.+\n(Sent|Отправлено|Date|Дата):\s", re.M),
)
INVISIBLE_CHARS_RE = re.compile("[​‌‍⁠﻿\x00]")


@dataclass(frozen=True)
class ParsedAttachment:
    filename: str
    mime: str
    payload: bytes = field(repr=False)

    @property
    def size(self) -> int:
        return len(self.payload)


@dataclass(frozen=True)
class ParsedEmail:
    message_id: str
    in_reply_to: str | None
    references: list[str]
    from_addr: str
    from_name: str | None
    to: list[dict[str, str]]
    cc: list[dict[str, str]]
    subject: str
    sent_at: datetime | None
    body_text: str
    headers: dict[str, str]
    attachments: list[ParsedAttachment]


def parse_email(raw: bytes, *, fallback_message_id: str) -> ParsedEmail:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    assert isinstance(msg, EmailMessage)

    from_name, from_addr = parseaddr(_header(msg, "From"))
    headers = {name: value for name in KEPT_HEADERS if (value := _header(msg, name))}
    body, hidden_text = _body_text(msg)
    if hidden_text:
        headers[HIDDEN_TEXT_HEADER] = hidden_text[:MAX_HIDDEN_CHARS]

    return ParsedEmail(
        message_id=_first_msgid(_header(msg, "Message-ID")) or fallback_message_id,
        in_reply_to=_first_msgid(_header(msg, "In-Reply-To")),
        references=_msgids(_header(msg, "References")),
        from_addr=from_addr.casefold(),
        from_name=_clean(from_name) or None,
        to=_addresses(msg, "To"),
        cc=_addresses(msg, "Cc"),
        subject=_header(msg, "Subject"),
        sent_at=_date(_header(msg, "Date")),
        body_text=body[:MAX_BODY_CHARS],
        headers=headers,
        attachments=_attachments(msg),
    )


def html_to_text(html: str) -> tuple[str, str]:
    """Текст из HTML без скриптов, цитат и скрытых блоков. Второе значение — скрытый текст."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head", "title", "noscript"]):
        tag.decompose()

    hidden = soup.find_all(style=HIDDEN_STYLE_RE)
    hidden_text = _clean_text("\n".join(tag.get_text(" ", strip=True) for tag in hidden))
    _decompose(hidden)
    for selector in QUOTE_SELECTORS:
        _decompose(soup.select(selector))
    for selector in QUOTE_START_SELECTORS:
        if (start := soup.select_one(selector)) is not None:
            _decompose([*start.find_next_siblings(), start])

    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(BLOCK_TAGS):
        block.insert_after("\n")
    return _clean_text(soup.get_text()), hidden_text


def strip_quoted_text(text: str) -> str:
    """Убирает цитату предыдущего письма.

    Пересланное содержимое оставляет: в пересылке оно и есть суть письма.
    """
    forward = FORWARD_RE.search(text)
    limit = forward.start() if forward else len(text)
    starts = (m.start() for pattern in REPLY_HEADER_RES if (m := pattern.search(text, 0, limit)))
    cut = min(starts, default=len(text))
    lines = [line for line in text[:cut].split("\n") if not line.startswith(">")]
    return _clean_text("\n".join(lines)) or text


def _decompose(tags: list[Tag]) -> None:
    for tag in tags:
        if not tag.decomposed:
            tag.decompose()


def _header(msg: EmailMessage, name: str) -> str:
    try:
        value = msg.get(name)
        return "" if value is None else _clean(str(value))
    except (ValueError, IndexError, TypeError, AttributeError):  # битые заголовки
        return ""


def _msgids(value: str) -> list[str]:
    return re.findall(r"<[^<>\s]+>", value)


def _first_msgid(value: str) -> str | None:
    ids = _msgids(value)
    if ids:
        return ids[0]
    return f"<{value}>" if value and " " not in value else None


def _addresses(msg: EmailMessage, name: str) -> list[dict[str, str]]:
    try:
        values = [str(v) for v in msg.get_all(name) or []]
    except (ValueError, IndexError, TypeError, AttributeError):
        return []
    return [
        {"name": _clean(display), "address": address.casefold()}
        for display, address in getaddresses(values)
        if address
    ]


def _date(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _body_text(msg: EmailMessage) -> tuple[str, str]:
    # HTML важнее plain: в нём размечены цитаты и виден скрытый текст
    html_part = msg.get_body(preferencelist=("html",))
    if html_part is not None:
        text, hidden_text = html_to_text(_part_text(html_part))
        if text:
            return strip_quoted_text(text), hidden_text
    plain_part = msg.get_body(preferencelist=("plain",))
    if plain_part is not None:
        return strip_quoted_text(_clean_text(_part_text(plain_part))), ""
    return "", ""


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except (LookupError, UnicodeError, AssertionError):  # неизвестная или неверная кодировка
        payload = part.get_payload(decode=True)
        return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


def _attachments(msg: EmailMessage) -> list[ParsedAttachment]:
    result = []
    for part in msg.iter_attachments():
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if not filename and disposition != "attachment":
            continue
        if (
            disposition == "inline"
            and part["Content-ID"]
            and part.get_content_maintype() == "image"
        ):
            continue  # логотипы в подписи
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload:
            result.append(
                ParsedAttachment(
                    filename=_clean(filename or "attachment"),
                    mime=part.get_content_type(),
                    payload=payload,
                )
            )
    return result


def _clean(value: str) -> str:
    return INVISIBLE_CHARS_RE.sub("", value).strip()


def _clean_text(text: str) -> str:
    text = INVISIBLE_CHARS_RE.sub("", text).replace("\xa0", " ").replace("\r\n", "\n")
    text = "\n".join(line.strip() for line in text.split("\n"))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
