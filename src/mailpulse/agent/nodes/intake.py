from langgraph.runtime import Runtime

from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.state import EmailState


async def load_email(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    email = await runtime.context.store.load_email(state["message_id"])
    return {"email": email, "attachments": [], "draft_iterations": 0}


async def extract_attachments(state: EmailState, runtime: Runtime[AgentDeps]) -> dict:
    return {"attachments": await runtime.context.extractor.extract(state["message_id"])}
