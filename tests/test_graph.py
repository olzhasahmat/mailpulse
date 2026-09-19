import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from mailpulse.agent.graph import MAX_DRAFT_ITERATIONS, build_graph, thread_config
from mailpulse.agent.ports import AgentDeps
from tests.fakes import Recorder, make_deps, make_email

MESSAGE_ID = 42


@pytest.fixture
def graph() -> CompiledStateGraph:
    return build_graph(checkpointer=InMemorySaver())


async def start(graph: CompiledStateGraph, deps: AgentDeps) -> dict:
    config = thread_config(MESSAGE_ID)
    await graph.ainvoke({"user_id": 1, "message_id": MESSAGE_ID}, config, context=deps)
    return config


async def resume(graph: CompiledStateGraph, deps: AgentDeps, config: dict, **answer) -> None:
    await graph.ainvoke(Command(resume=answer), config, context=deps)


async def paused_at(graph: CompiledStateGraph, config: dict) -> tuple[str, ...]:
    return (await graph.aget_state(config)).next


async def test_noise_ends_without_notification(graph):
    rec = Recorder()
    config = await start(graph, make_deps(rec, importance=0))

    assert await paused_at(graph, config) == ()
    assert "save_triage" in rec.names()
    assert "send_card" not in rec.names()
    assert "enqueue_digest" not in rec.names()


async def test_fyi_goes_to_digest(graph):
    rec = Recorder()
    config = await start(graph, make_deps(rec, importance=1))

    assert await paused_at(graph, config) == ()
    assert rec.args("enqueue_digest") == [(MESSAGE_ID,)]
    assert "send_card" not in rec.names()


async def test_suspicious_email_is_quarantined_before_classification(graph):
    rec = Recorder()
    await start(graph, make_deps(rec, guard_suspicious=True))

    assert "send_quarantine" in rec.names()
    assert "classify" not in rec.names()


async def test_classifier_can_quarantine_too(graph):
    rec = Recorder()
    await start(graph, make_deps(rec, importance=3, triage_suspicious=True))

    [(verdict,)] = rec.args("send_quarantine")
    assert verdict == {"suspicious": True, "reasons": ["тест"]}
    assert "send_card" not in rec.names()


async def test_muted_sender_skips_llm_calls(graph):
    rec = Recorder()
    await start(graph, make_deps(rec, rule="mute"))

    assert "retrieve" not in rec.names()
    assert "classify" not in rec.names()


async def test_vip_rule_raises_importance(graph):
    rec = Recorder()
    config = await start(graph, make_deps(rec, rule="vip", importance=0))

    [(triage,)] = rec.args("save_triage")
    assert triage["importance"] == 2
    assert await paused_at(graph, config) == ("await_action",)


@pytest.mark.parametrize("has_attachments", [True, False])
async def test_attachments_are_extracted_only_when_present(graph, has_attachments):
    rec = Recorder()
    await start(graph, make_deps(rec, email=make_email(has_attachments=has_attachments)))

    assert ("extract" in rec.names()) is has_attachments


async def test_card_is_sent_once_across_resume(graph):
    rec = Recorder()
    deps = make_deps(rec, importance=3)
    config = await start(graph, deps)
    assert await paused_at(graph, config) == ("await_action",)

    await resume(graph, deps, config, action="not_important")

    assert rec.args("send_card") == [(MESSAGE_ID,)]
    assert rec.args("save_feedback") == [("importance_wrong", {"importance": 3})]
    assert await paused_at(graph, config) == ()


async def test_draft_edit_loop_then_send(graph):
    rec = Recorder()
    deps = make_deps(rec)
    config = await start(graph, deps)

    await resume(graph, deps, config, action="draft")
    assert await paused_at(graph, config) == ("review_draft",)
    assert rec.args("send_draft_card") == [(501, "черновик v1")]

    await resume(graph, deps, config, action="edit", text="короче")
    assert await paused_at(graph, config) == ("review_draft",)
    assert rec.args("draft")[-1] == ("черновик v1", "короче")
    assert [version for _, version in rec.args("save_draft")] == [1, 2]

    await resume(graph, deps, config, action="send")
    assert await paused_at(graph, config) == ()
    names = rec.names()
    assert names.index("approve_draft") < names.index("outbox_send")
    assert rec.args("outbox_send") == [(502,)]


async def test_edit_limit_stops_the_loop(graph):
    rec = Recorder()
    deps = make_deps(rec)
    config = await start(graph, deps)
    await resume(graph, deps, config, action="draft")

    for _ in range(MAX_DRAFT_ITERATIONS):
        await resume(graph, deps, config, action="edit", text="ещё раз")

    assert len(rec.args("draft")) == MAX_DRAFT_ITERATIONS
    assert await paused_at(graph, config) == ()
    assert "outbox_send" not in rec.names()


async def test_unknown_resume_action_is_rejected(graph):
    rec = Recorder()
    deps = make_deps(rec)
    config = await start(graph, deps)

    with pytest.raises(ValueError, match="unexpected resume payload"):
        await resume(graph, deps, config, action="forward_to_everyone")


async def test_sender_history_reaches_classifier(graph):
    rec = Recorder()
    deps = make_deps(rec)
    profile = {
        "address": "boss@company.kz",
        "msg_count": 14,
        "reply_count": 9,
        "last_contact_at": None,
    }
    deps.store.sender = profile

    await start(graph, deps)

    assert rec.args("classify") == [(profile,)]
