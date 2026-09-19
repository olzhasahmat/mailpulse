"""Фейковые реализации портов графа: записывают вызовы и отдают заданные ответы."""

from dataclasses import dataclass, field
from typing import Any

from mailpulse.agent.ports import AgentDeps
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


@dataclass
class Recorder:
    calls: list[tuple[str, tuple[Any, ...]]] = field(default_factory=list)

    def __call__(self, name: str, *args: Any) -> None:
        self.calls.append((name, args))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def args(self, name: str) -> list[tuple[Any, ...]]:
        return [args for called, args in self.calls if called == name]


def make_email(**overrides: Any) -> EmailView:
    email: dict[str, Any] = {
        "message_id": 42,
        "thread_id": None,
        "account_id": 1,
        "from_addr": "boss@company.kz",
        "from_name": "Руководитель",
        "subject": "Отчёт к пятнице",
        "body_text": "Пришлите, пожалуйста, отчёт до пятницы.",
        "sent_at": "2026-09-12T09:00:00+05:00",
        "has_attachments": False,
        "is_test": False,
        "headers": {},
    }
    return {**email, **overrides}  # type: ignore[return-value]


@dataclass
class FakeStore:
    rec: Recorder
    email: EmailView
    rule: RuleHit | None = None
    drafts_saved: int = 0
    sender: SenderProfile | None = None

    async def load_email(self, message_id: int) -> EmailView:
        self.rec("load_email", message_id)
        return self.email

    async def rule_for_sender(self, user_id: int, address: str) -> RuleHit | None:
        self.rec("rule_for_sender", address)
        return self.rule

    async def sender_profile(self, user_id: int, address: str) -> SenderProfile | None:
        return self.sender

    async def save_triage(self, message_id: int, triage: TriageResult) -> None:
        self.rec("save_triage", triage)

    async def record_llm_usage(self, user_id: int, node: str, meta: CallMeta) -> None:
        self.rec("record_llm_usage", node, meta["model"])

    async def enqueue_digest(self, user_id: int, message_id: int) -> None:
        self.rec("enqueue_digest", message_id)

    async def save_draft(self, message_id: int, body: str, version: int) -> int:
        self.drafts_saved += 1
        self.rec("save_draft", body, version)
        return 500 + self.drafts_saved

    async def approve_draft(self, draft_id: int) -> None:
        self.rec("approve_draft", draft_id)

    async def save_feedback(
        self, user_id: int, message_id: int, kind: str, value: dict[str, Any]
    ) -> None:
        self.rec("save_feedback", kind, value)


@dataclass
class FakeExtractor:
    rec: Recorder

    async def extract(self, message_id: int) -> list[AttachmentText]:
        self.rec("extract", message_id)
        return [
            {"attachment_id": 1, "filename": "scan.jpg", "text": "245 000 ₸", "method": "vision"}
        ]


@dataclass
class FakeGuard:
    rec: Recorder
    suspicious: bool = False

    async def check(self, email: EmailView, attachments: list[AttachmentText]) -> GuardrailVerdict:
        self.rec("guard", attachments)
        return {"suspicious": self.suspicious, "reasons": ["injection"] if self.suspicious else []}


@dataclass
class FakeRetriever:
    rec: Recorder

    async def retrieve(self, user_id: int, email: EmailView) -> list[RetrievedChunk]:
        self.rec("retrieve", user_id)
        return []


@dataclass
class FakeClassifier:
    rec: Recorder
    importance: int = 2
    suspicious: bool = False
    fail_times: int = 0

    async def classify(
        self,
        email: EmailView,
        attachments: list[AttachmentText],
        context: list[RetrievedChunk],
        sender: SenderProfile | None,
    ) -> TriageResult:
        self.rec("classify", sender)
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("LLM недоступна")
        return {
            "reasoning": "тест",
            "importance": self.importance,
            "category": "work",
            "needs_reply": True,
            "suspicious": self.suspicious,
            "extracted": {},
        }


@dataclass
class FakeSummarizer:
    rec: Recorder

    async def summarize(
        self,
        email: EmailView,
        attachments: list[AttachmentText],
        context: list[RetrievedChunk],
    ) -> Summary:
        self.rec("summarize")
        return {"tldr": "Нужен отчёт", "amounts": [], "deadlines": [], "action_items": []}


@dataclass
class FakeDrafter:
    rec: Recorder

    async def draft(
        self,
        email: EmailView,
        context: list[RetrievedChunk],
        previous: str | None,
        feedback: str | None,
    ) -> tuple[str, None]:
        self.rec("draft", previous, feedback)
        return f"черновик v{len(self.rec.args('draft'))}", None


@dataclass
class FakeNotifier:
    rec: Recorder

    async def send_card(
        self,
        user_id: int,
        message_id: int,
        email: EmailView,
        triage: TriageResult,
        summary: Summary,
    ) -> None:
        self.rec("send_card", message_id)

    async def send_draft(self, user_id: int, message_id: int, draft_id: int, body: str) -> None:
        self.rec("send_draft_card", draft_id, body)

    async def send_quarantine(
        self, user_id: int, message_id: int, email: EmailView, verdict: GuardrailVerdict
    ) -> None:
        self.rec("send_quarantine", verdict)


@dataclass
class FakeOutbox:
    rec: Recorder

    async def send(self, draft_id: int) -> None:
        self.rec("outbox_send", draft_id)


def make_deps(
    rec: Recorder,
    *,
    email: EmailView | None = None,
    rule: RuleHit | None = None,
    guard_suspicious: bool = False,
    importance: int = 2,
    triage_suspicious: bool = False,
    classifier_fail_times: int = 0,
) -> AgentDeps:
    return AgentDeps(
        store=FakeStore(rec, email or make_email(), rule),
        extractor=FakeExtractor(rec),
        guard=FakeGuard(rec, guard_suspicious),
        retriever=FakeRetriever(rec),
        classifier=FakeClassifier(rec, importance, triage_suspicious, classifier_fail_times),
        summarizer=FakeSummarizer(rec),
        drafter=FakeDrafter(rec),
        notifier=FakeNotifier(rec),
        outbox=FakeOutbox(rec),
    )
