"""Очередь задач на Postgres: FOR UPDATE SKIP LOCKED вместо Redis/Celery (DECISIONS.md, ADR-002).

Функции не коммитят: вызывающий код решает, где граница транзакции. Так письмо и задача
на его обработку записываются атомарно.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from mailpulse.db.models import Job, JobStatus

MAX_BACKOFF = timedelta(minutes=15)
_NO_SYNC = {"synchronize_session": False}


@dataclass(frozen=True)
class ClaimedJob:
    id: int
    type: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int


async def enqueue(
    session: AsyncSession,
    job_type: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 5,
) -> int:
    stmt = (
        insert(Job)
        .values(type=job_type, payload=payload, max_attempts=max_attempts)
        .returning(Job.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def claim(session: AsyncSession, types: Collection[str]) -> ClaimedJob | None:
    """Берёт задачу только из переданных типов: остальные ждут воркер, который их умеет."""
    next_id = (
        select(Job.id)
        .where(
            Job.status == JobStatus.QUEUED,
            Job.run_after <= func.now(),
            Job.type.in_(list(types)),
        )
        .order_by(Job.run_after, Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    stmt = (
        update(Job)
        .where(Job.id == next_id)
        .values(status=JobStatus.RUNNING, locked_at=func.now(), attempts=Job.attempts + 1)
        .returning(Job.id, Job.type, Job.payload, Job.attempts, Job.max_attempts)
        .execution_options(**_NO_SYNC)
    )
    row = (await session.execute(stmt)).one_or_none()
    return ClaimedJob(*row) if row else None


async def complete(session: AsyncSession, job_id: int) -> None:
    stmt = (
        update(Job)
        .where(Job.id == job_id)
        .values(status=JobStatus.DONE, locked_at=None)
        .execution_options(**_NO_SYNC)
    )
    await session.execute(stmt)


async def fail(session: AsyncSession, job: ClaimedJob, error: str, *, retry: bool = True) -> None:
    if retry and job.attempts < job.max_attempts:
        backoff = min(timedelta(seconds=5 * 2**job.attempts), MAX_BACKOFF)
        values: dict[str, Any] = {"status": JobStatus.QUEUED, "run_after": func.now() + backoff}
    else:
        values = {"status": JobStatus.FAILED}
    stmt = (
        update(Job)
        .where(Job.id == job.id)
        .values(**values, locked_at=None, last_error=error[:4000])
        .execution_options(**_NO_SYNC)
    )
    await session.execute(stmt)


async def requeue_stale(session: AsyncSession, older_than: timedelta) -> int:
    """Возвращает в очередь задачи, чей воркер упал, не успев завершить их."""
    stmt = (
        update(Job)
        .where(Job.status == JobStatus.RUNNING, Job.locked_at < func.now() - older_than)
        .values(status=JobStatus.QUEUED, locked_at=None)
        .execution_options(**_NO_SYNC)
    )
    return (await session.execute(stmt)).rowcount
