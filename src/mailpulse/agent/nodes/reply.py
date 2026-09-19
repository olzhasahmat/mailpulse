from langgraph.runtime import Runtime

from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.state import EmailState


async def draft_reply(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    deps = runtime.context
    iteration = state.get("draft_iterations", 0) + 1
    body, meta = await deps.drafter.draft(
        state["email"],
        state.get("context", []),
        previous=state.get("draft"),
        feedback=state.get("user_feedback"),
    )
    draft_id = await deps.store.save_draft(state["message_id"], body, version=iteration)
    if meta:
        await deps.store.record_llm_usage(state["user_id"], "draft", meta)
    return {"draft": body, "draft_id": draft_id, "draft_iterations": iteration}


async def send_reply(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    await runtime.context.outbox.send(state["draft_id"])
    return {}
