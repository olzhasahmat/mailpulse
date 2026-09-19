from langgraph.runtime import Runtime

from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.state import EmailState, GuardrailVerdict


async def guardrail(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    verdict = await runtime.context.guard.check(state["email"], state.get("attachments", []))
    return {"guardrail": verdict}


async def quarantine_notify(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    verdict: GuardrailVerdict = state["guardrail"]
    triage = state.get("triage")
    if not verdict["suspicious"] and triage and triage["suspicious"]:
        verdict = {"suspicious": True, "reasons": [triage["reasoning"]]}
    await runtime.context.notifier.send_quarantine(
        state["user_id"], state["message_id"], state["email"], verdict
    )
    return {}


async def apply_rules(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    hit = await runtime.context.store.rule_for_sender(state["user_id"], state["email"]["from_addr"])
    return {"rule_hit": hit}
