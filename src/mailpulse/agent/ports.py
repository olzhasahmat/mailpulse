"""Порты графа: всё, что узлы получают снаружи.

Реализации меняются без изменения графа: фейки в тестах, LLM и Postgres в работе.
"""

from dataclasses import dataclass
from typing import Any, Protocol

from mailpulse.agent.state import (
    AttachmentText,
    CallMeta,
    EmailView,
    GuardrailVerdict,
    RetrievedChunk,
    RuleHit,
    SenderProfile,
    Summary,
    TriageResult,
)


class MailStore(Protocol):
    async def load_email(self, message_id: int) -> EmailView: ...
    async def rule_for_sender(self, user_id: int, address: str) -> RuleHit | None: ...
    async def sender_profile(self, user_id: int, address: str) -> SenderProfile | None: ...
    async def save_triage(self, message_id: int, triage: TriageResult) -> None: ...
    async def enqueue_digest(self, user_id: int, message_id: int) -> None: ...
    async def save_draft(self, message_id: int, body: str, version: int) -> int: ...
    async def approve_draft(self, draft_id: int) -> None: ...
    async def record_llm_usage(self, user_id: int, node: str, meta: CallMeta) -> None: ...
    async def save_feedback(
        self, user_id: int, message_id: int, kind: str, value: dict[str, Any]
    ) -> None: ...


class Extractor(Protocol):
    """Текст вложений: текстовый слой PDF, DOCX, HTML, vision для сканов. Результат кэшируется."""

    async def extract(self, message_id: int) -> list[AttachmentText]: ...


class Guard(Protocol):
    async def check(
        self, email: EmailView, attachments: list[AttachmentText]
    ) -> GuardrailVerdict: ...


class Retriever(Protocol):
    async def retrieve(self, user_id: int, email: EmailView) -> list[RetrievedChunk]: ...


class Classifier(Protocol):
    async def classify(
        self,
        email: EmailView,
        attachments: list[AttachmentText],
        context: list[RetrievedChunk],
        sender: SenderProfile | None,
    ) -> TriageResult: ...


class Summarizer(Protocol):
    async def summarize(
        self,
        email: EmailView,
        attachments: list[AttachmentText],
        context: list[RetrievedChunk],
    ) -> Summary: ...


class Drafter(Protocol):
    async def draft(
        self,
        email: EmailView,
        context: list[RetrievedChunk],
        previous: str | None,
        feedback: str | None,
    ) -> tuple[str, CallMeta | None]: ...


class Notifier(Protocol):
    async def send_card(
        self,
        user_id: int,
        message_id: int,
        email: EmailView,
        triage: TriageResult,
        summary: Summary,
    ) -> None: ...

    async def send_draft(self, user_id: int, message_id: int, draft_id: int, body: str) -> None: ...

    async def send_quarantine(
        self, user_id: int, message_id: int, email: EmailView, verdict: GuardrailVerdict
    ) -> None: ...


class Outbox(Protocol):
    """Отправка одобренного черновика (через MCP-tool send_draft)."""

    async def send(self, draft_id: int) -> None: ...


@dataclass(frozen=True)
class AgentDeps:
    store: MailStore
    extractor: Extractor
    guard: Guard
    retriever: Retriever
    classifier: Classifier
    summarizer: Summarizer
    drafter: Drafter
    notifier: Notifier
    outbox: Outbox
