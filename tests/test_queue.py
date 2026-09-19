from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from mailpulse.db.models import Job, JobStatus
from mailpulse.worker import queue
from mailpulse.worker.main import process_next

pytestmark = pytest.mark.db


async def noop(payload):
    pass


async def get_job(db, job_id: int) -> Job:
    async with db() as session:
        return (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()


async def test_claim_takes_job_once(db):
    async with db.begin() as session:
        job_id = await queue.enqueue(session, "ping", {"n": 1})

    async with db.begin() as session:
        job = await queue.claim(session, ["ping"])
    async with db.begin() as session:
        again = await queue.claim(session, ["ping"])

    assert job is not None
    assert (job.id, job.attempts, job.payload) == (job_id, 1, {"n": 1})
    assert again is None


async def test_concurrent_claims_skip_locked_rows(db):
    async with db.begin() as session:
        for n in range(2):
            await queue.enqueue(session, "ping", {"n": n})

    async with db.begin() as first_session, db.begin() as second_session:
        first = await queue.claim(first_session, ["ping"])
        second = await queue.claim(second_session, ["ping"])

    assert first is not None and second is not None
    assert first.id != second.id


async def test_failed_job_is_retried_then_marked_failed(db):
    async def boom(payload):
        raise RuntimeError("boom")

    async with db.begin() as session:
        job_id = await queue.enqueue(session, "boom", {}, max_attempts=2)

    assert await process_next(db, {"boom": boom})
    job = await get_job(db, job_id)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 1)
    assert "boom" in job.last_error

    async with db.begin() as session:
        await session.execute(update(Job).where(Job.id == job_id).values(run_after=func.now()))
    assert await process_next(db, {"boom": boom})

    job = await get_job(db, job_id)
    assert (job.status, job.attempts) == (JobStatus.FAILED, 2)


async def test_jobs_without_handler_stay_queued(db):
    async with db.begin() as session:
        job_id = await queue.enqueue(session, "future_feature", {})

    assert not await process_next(db, {"ping": noop})

    job = await get_job(db, job_id)
    assert (job.status, job.attempts) == (JobStatus.QUEUED, 0)


async def test_stale_running_job_is_requeued(db):
    async with db.begin() as session:
        job_id = await queue.enqueue(session, "ping", {})
    async with db.begin() as session:
        await queue.claim(session, ["ping"])
        await session.execute(
            update(Job).where(Job.id == job_id).values(locked_at=func.now() - timedelta(hours=1))
        )

    async with db.begin() as session:
        requeued = await queue.requeue_stale(session, timedelta(minutes=10))

    assert requeued == 1
    assert (await get_job(db, job_id)).status == JobStatus.QUEUED
