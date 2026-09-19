"""Граф обработки одного письма. Схема и путь запроса — в ARCHITECTURE.md."""

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from mailpulse.agent.nodes.intake import extract_attachments, load_email
from mailpulse.agent.nodes.interaction import (
    await_action,
    record_feedback,
    review_draft,
    send_card,
    send_draft_card,
)
from mailpulse.agent.nodes.reply import draft_reply, send_reply
from mailpulse.agent.nodes.safety import apply_rules, guardrail, quarantine_notify
from mailpulse.agent.nodes.triage import classify, digest_enqueue, retrieve, summarize
from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.state import EmailState

MAX_DRAFT_ITERATIONS = 3


def route_after_load(state: EmailState) -> str:
    return "extract_attachments" if state["email"]["has_attachments"] else "guardrail"


def route_guardrail(state: EmailState) -> str:
    return "quarantine_notify" if state["guardrail"]["suspicious"] else "apply_rules"


def route_rules(state: EmailState) -> str:
    return END if state.get("rule_hit") == "mute" else "retrieve"


def route_importance(state: EmailState) -> str:
    triage = state["triage"]
    if triage["suspicious"]:
        return "quarantine_notify"
    if triage["importance"] >= 2:
        return "summarize"
    if triage["importance"] == 1:
        return "digest_enqueue"
    return END


def route_action(state: EmailState) -> str:
    return "draft_reply" if state["decision"] == "draft" else "record_feedback"


def route_review(state: EmailState) -> str:
    match state["decision"]:
        case "send":
            return "send_reply"
        case "edit" if state["draft_iterations"] < MAX_DRAFT_ITERATIONS:
            return "draft_reply"
        case _:
            # cancel или исчерпан лимит правок; бот видит draft_iterations и сообщает о лимите
            return END


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    builder = StateGraph(EmailState, context_schema=AgentDeps)
    for node in (
        load_email,
        extract_attachments,
        guardrail,
        quarantine_notify,
        apply_rules,
        retrieve,
        classify,
        digest_enqueue,
        summarize,
        send_card,
        await_action,
        record_feedback,
        draft_reply,
        send_draft_card,
        review_draft,
        send_reply,
    ):
        builder.add_node(node)

    builder.add_edge(START, "load_email")
    builder.add_conditional_edges(
        "load_email", route_after_load, ["extract_attachments", "guardrail"]
    )
    builder.add_edge("extract_attachments", "guardrail")
    builder.add_conditional_edges(
        "guardrail", route_guardrail, ["quarantine_notify", "apply_rules"]
    )
    builder.add_conditional_edges("apply_rules", route_rules, ["retrieve", END])
    builder.add_edge("retrieve", "classify")
    builder.add_conditional_edges(
        "classify", route_importance, ["summarize", "digest_enqueue", "quarantine_notify", END]
    )
    builder.add_edge("summarize", "send_card")
    builder.add_edge("send_card", "await_action")
    builder.add_conditional_edges("await_action", route_action, ["draft_reply", "record_feedback"])
    builder.add_edge("draft_reply", "send_draft_card")
    builder.add_edge("send_draft_card", "review_draft")
    builder.add_conditional_edges("review_draft", route_review, ["send_reply", "draft_reply", END])
    for terminal in ("quarantine_notify", "digest_enqueue", "record_feedback", "send_reply"):
        builder.add_edge(terminal, END)

    return builder.compile(checkpointer=checkpointer)


def thread_config(message_id: int) -> dict:
    """Один тред чекпоинтера на письмо: по нему бот возобновляет граф после нажатия кнопки."""
    return {"configurable": {"thread_id": f"email-{message_id}"}}


if __name__ == "__main__":
    print(build_graph().get_graph().draw_mermaid())
