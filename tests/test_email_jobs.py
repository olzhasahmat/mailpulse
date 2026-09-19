import contextlib

from langgraph.checkpoint.memory import InMemorySaver

from mailpulse.agent.graph import build_graph
from mailpulse.worker.email_jobs import EmailJobs
from tests.fakes import Recorder, make_deps

MESSAGE_ID = 7


def make_jobs(rec: Recorder, owner: int | None = 1, **deps_options) -> EmailJobs:
    async def owner_of(message_id: int) -> int | None:
        return owner

    return EmailJobs(build_graph(InMemorySaver()), make_deps(rec, **deps_options), owner_of)


async def test_duplicate_process_job_does_not_resend_card():
    rec = Recorder()
    jobs = make_jobs(rec)

    await jobs.process_email({"message_id": MESSAGE_ID})
    await jobs.process_email({"message_id": MESSAGE_ID})

    assert rec.args("send_card") == [(MESSAGE_ID,)]


async def test_resume_finishes_graph_and_repeated_press_is_ignored():
    rec = Recorder()
    jobs = make_jobs(rec)
    await jobs.process_email({"message_id": MESSAGE_ID})

    answer = {"message_id": MESSAGE_ID, "answer": {"action": "read"}}
    await jobs.resume_email(answer)
    await jobs.resume_email(answer)

    assert rec.args("save_feedback") == [("importance_right", {"importance": 2})]


async def test_resume_without_waiting_graph_is_noop():
    rec = Recorder()

    await make_jobs(rec).resume_email({"message_id": 999, "answer": {"action": "read"}})

    assert rec.calls == []


async def test_message_without_owner_is_skipped():
    rec = Recorder()

    await make_jobs(rec, owner=None).process_email({"message_id": MESSAGE_ID})

    assert rec.calls == []


async def test_retry_after_llm_failure_continues_from_checkpoint():
    rec = Recorder()
    jobs = make_jobs(rec, classifier_fail_times=1)

    with contextlib.suppress(RuntimeError):
        await jobs.process_email({"message_id": MESSAGE_ID})
    await jobs.process_email({"message_id": MESSAGE_ID})

    assert len(rec.args("load_email")) == 1
    assert len(rec.args("classify")) == 2
    assert rec.args("send_card") == [(MESSAGE_ID,)]
