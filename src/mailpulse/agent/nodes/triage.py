from langgraph.runtime import Runtime

from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.state import EmailState

VIP_MIN_IMPORTANCE = 2


async def retrieve(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    deps = runtime.context
    email = state["email"]
    return {
        "context": await deps.retriever.retrieve(state["user_id"], email),
        "sender": await deps.store.sender_profile(state["user_id"], email["from_addr"]),
    }


async def classify(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    deps = runtime.context
    triage = await deps.classifier.classify(
        state["email"],
        state.get("attachments", []),
        state.get("context", []),
        state.get("sender"),
    )
    if state.get("rule_hit") == "vip" and triage["importance"] < VIP_MIN_IMPORTANCE:
        # Правило пользователя применяется кодом, а не просьбой в промпте: модель может его нарушить
        triage = {**triage, "importance": VIP_MIN_IMPORTANCE}
    await deps.store.save_triage(state["message_id"], triage)
    if meta := triage.get("meta"):
        await deps.store.record_llm_usage(state["user_id"], "classify", meta)
    return {"triage": triage}


async def digest_enqueue(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    await runtime.context.store.enqueue_digest(state["user_id"], state["message_id"])
    return {}


async def summarize(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    summary = await runtime.context.summarizer.summarize(
        state["email"], state.get("attachments", []), state.get("context", [])
    )
    if meta := summary.get("meta"):
        await runtime.context.store.record_llm_usage(state["user_id"], "summarize", meta)
    return {"summary": summary}
