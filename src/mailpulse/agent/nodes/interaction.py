"""Узлы общения с пользователем в Telegram.

Отправка и ожидание ответа — всегда разные узлы. После resume узел с interrupt()
выполняется заново с начала, и отправка в том же узле продублировала бы сообщение.
"""

from typing import Any

from langgraph.runtime import Runtime
from langgraph.types import interrupt

from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.state import EmailState

CARD_ACTIONS = frozenset({"draft", "read", "not_important"})
REVIEW_ACTIONS = frozenset({"send", "edit", "cancel"})


def _read_answer(answer: Any, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(answer, dict) or answer.get("action") not in allowed:
        raise ValueError(
            f"unexpected resume payload {answer!r}, expected action in {sorted(allowed)}"
        )
    return answer


async def send_card(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    await runtime.context.notifier.send_card(
        state["user_id"], state["message_id"], state["email"], state["triage"], state["summary"]
    )
    return {}


async def await_action(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    answer = _read_answer(
        interrupt({"kind": "card", "message_id": state["message_id"]}), CARD_ACTIONS
    )
    return {"decision": answer["action"]}


async def record_feedback(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    kind = "importance_wrong" if state["decision"] == "not_important" else "importance_right"
    await runtime.context.store.save_feedback(
        state["user_id"], state["message_id"], kind, {"importance": state["triage"]["importance"]}
    )
    return {}


async def send_draft_card(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    await runtime.context.notifier.send_draft(
        state["user_id"], state["message_id"], state["draft_id"], state["draft"]
    )
    return {}


async def review_draft(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    payload = {"kind": "draft", "message_id": state["message_id"], "draft_id": state["draft_id"]}
    answer = _read_answer(interrupt(payload), REVIEW_ACTIONS)
    if answer["action"] == "send":
        # Одобрение фиксируется там, где его дал человек; send_draft без него откажет
        await runtime.context.store.approve_draft(state["draft_id"])
    return {"decision": answer["action"], "user_feedback": answer.get("text")}
