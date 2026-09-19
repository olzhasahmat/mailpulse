"""Состояние графа обработки одного письма.

Только JSON-совместимые типы: состояние лежит в чекпоинтере Postgres между прерываниями,
и произвольные классы там сериализовать рискованно (DECISIONS.md, ADR-006).
"""

from typing import Any, Literal, NotRequired, Required, TypedDict

Decision = Literal["draft", "read", "not_important", "send", "edit", "cancel"]
RuleHit = Literal["vip", "mute"]


class EmailView(TypedDict):
    message_id: int
    thread_id: int | None
    account_id: int
    from_addr: str
    from_name: str | None
    subject: str
    body_text: str
    sent_at: str | None
    has_attachments: bool
    is_test: bool
    headers: dict[str, str]


class AttachmentText(TypedDict):
    attachment_id: int
    filename: str
    text: str
    method: str


class GuardrailVerdict(TypedDict):
    suspicious: bool
    reasons: list[str]


class RetrievedChunk(TypedDict):
    chunk_id: int | None  # None — письмо из текущего треда, а не результат поиска
    message_id: int
    content: str
    score: float
    source: Literal["thread", "similar"]


class SenderProfile(TypedDict):
    address: str
    msg_count: int  # писем с адреса, включая текущее
    reply_count: int  # ответов пользователя на них
    last_contact_at: str | None


class CallMeta(TypedDict):
    """Сведения о вызове LLM: для triage_results, llm_usage и A/B."""

    model: str
    prompt_version: str
    latency_ms: int
    cost_usd: float
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class TriageResult(TypedDict):
    reasoning: str
    importance: int  # 0 шум, 1 к сведению, 2 важно, 3 срочно
    category: str
    needs_reply: bool
    suspicious: bool
    extracted: dict[str, Any]
    meta: NotRequired[CallMeta]


class Summary(TypedDict):
    tldr: str
    amounts: list[dict[str, Any]]
    deadlines: list[str]
    action_items: list[str]
    meta: NotRequired[CallMeta]


class EmailState(TypedDict, total=False):
    user_id: Required[int]
    message_id: Required[int]
    email: EmailView
    attachments: list[AttachmentText]
    guardrail: GuardrailVerdict
    rule_hit: RuleHit | None
    context: list[RetrievedChunk]
    sender: SenderProfile | None
    triage: TriageResult
    summary: Summary
    draft_id: int
    draft: str
    draft_iterations: int
    decision: Decision
    user_feedback: str | None
