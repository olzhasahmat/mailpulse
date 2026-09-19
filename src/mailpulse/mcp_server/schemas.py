"""Схемы ответов MCP-tool'ов: из них SDK строит outputSchema."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class EmailHit(BaseModel):
    message_id: int
    from_addr: str
    from_name: str | None
    subject: str
    sent_at: datetime
    snippet: str
    importance: int | None = Field(
        description="0 шум, 1 к сведению, 2 важно, 3 срочно; null — исходящее или ещё не разобрано"
    )


class AttachmentContent(BaseModel):
    attachment_id: int
    filename: str
    mime: str
    text: str | None = Field(description="Извлечённый текст; null, если текст не запрашивался")
    extraction_method: str | None


class ThreadMessage(BaseModel):
    message_id: int
    from_addr: str
    from_name: str | None
    sent_at: datetime
    subject: str
    body: str
    outgoing: bool = Field(description="Письмо отправил сам пользователь")
    attachments: list[AttachmentContent]


class Thread(BaseModel):
    thread_id: int
    messages: list[ThreadMessage]


class PendingItem(BaseModel):
    message_id: int
    from_addr: str
    subject: str
    sent_at: datetime
    importance: int
    reason: str
    deadline: date | None


class DraftInfo(BaseModel):
    draft_id: int
    message_id: int
    status: Literal["pending", "approved", "sent"]
    body: str
