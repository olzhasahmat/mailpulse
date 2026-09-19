"""Нарезка писем на чанки для RAG.

Каждый чанк начинается с заголовка «От / Кому / Тема / Дата»: без него фрагмент
«Да, давайте в пятницу» не находится ни по отправителю, ни по теме.
"""

import re
from dataclasses import dataclass
from datetime import datetime

# e5 обрезает вход на 512 токенах; кириллица в его токенизаторе ≈ 3–4 символа на токен,
# поэтому 1500 символов тела + заголовок укладываются в окно с запасом
MAX_BODY_CHARS = 1500
OVERLAP_CHARS = 200
PARAGRAPH_RE = re.compile(r"\n\s*\n")
SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True)
class IndexableMessage:
    from_addr: str
    from_name: str | None
    to: list[str]
    subject: str
    sent_at: datetime | None
    body: str
    outgoing: bool


def message_header(message: IndexableMessage) -> str:
    sender = (
        f"{message.from_name} <{message.from_addr}>" if message.from_name else message.from_addr
    )
    lines = [
        f"От: {sender}" + (" (пользователь)" if message.outgoing else ""),
        f"Кому: {', '.join(message.to)}" if message.to else None,
        f"Тема: {message.subject or '(без темы)'}",
        f"Дата: {message.sent_at:%Y-%m-%d}" if message.sent_at else None,
    ]
    return "\n".join(line for line in lines if line)


def build_chunks(message: IndexableMessage, *, with_header: bool = True) -> list[str]:
    """Чанки письма. with_header=False — вариант для A/B «с заголовком или без»."""
    header = message_header(message) if with_header else ""
    parts = split_body(message.body) or [""]
    chunks = [f"{header}\n\n{part}".strip() for part in parts]
    return [chunk for chunk in chunks if chunk]


def split_body(
    body: str, max_chars: int = MAX_BODY_CHARS, overlap: int = OVERLAP_CHARS
) -> list[str]:
    """Режет по абзацам, длинные абзацы — по предложениям; соседние чанки перекрываются."""
    body = body.strip()
    if len(body) <= max_chars:
        return [body] if body else []

    pieces = [
        piece
        for paragraph in PARAGRAPH_RE.split(body)
        if paragraph.strip()
        for piece in _split_long(paragraph.strip(), max_chars)
    ]
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}" if current else piece
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        tail = _tail(current, overlap)
        current = (
            f"{tail}\n\n{piece}" if tail and len(tail) + len(piece) + 2 <= max_chars else piece
        )
    if current:
        chunks.append(current)
    return chunks


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    current = ""
    for sentence in SENTENCE_END_RE.split(text):
        if len(sentence) > max_chars:
            if current:
                parts.append(current)
                current = ""
            parts.extend(_hard_split(sentence, max_chars))
            continue
        candidate = f"{current} {sentence}" if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
        else:
            parts.append(current)
            current = sentence
    if current:
        parts.append(current)
    return parts


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Предложение длиннее чанка (таблица, склеенный текст): режем по словам."""
    parts: list[str] = []
    current = ""
    for word in text.split():
        candidate = f"{current} {word}" if current else word
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
        current = word[:max_chars]
    if current:
        parts.append(current)
    return parts


def _tail(text: str, size: int) -> str:
    """Конец текста длиной до size символов, начиная с целого слова."""
    if len(text) <= size:
        return text
    tail = text[-size:]
    space = tail.find(" ")
    return tail[space + 1 :] if space != -1 else tail
