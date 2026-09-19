"""Воркер: забирает задачи из очереди и выполняет их."""

import asyncio
import contextlib
import logging
import signal
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.config import get_settings
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.observability import setup_logging
from mailpulse.worker import queue

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


async def _ping(payload: dict[str, Any]) -> None:
    log.info("ping: %s", payload)


BASE_HANDLERS: dict[str, Handler] = {"ping": _ping}


async def process_next(
    sessionmaker: async_sessionmaker[AsyncSession], handlers: dict[str, Handler]
) -> bool:
    """Выполняет одну задачу. Возвращает False, если подходящих задач нет."""
    async with sessionmaker.begin() as session:
        job = await queue.claim(session, handlers.keys())
    if job is None:
        return False

    try:
        await handlers[job.type](job.payload)
    except Exception as exc:
        log.exception("job %s (%s) failed", job.id, job.type)
        async with sessionmaker.begin() as session:
            await queue.fail(session, job, repr(exc))
    else:
        async with sessionmaker.begin() as session:
            await queue.complete(session, job.id)
    return True


async def run_worker(stop: asyncio.Event) -> None:
    # Импорт здесь: сборка графа тянет SDK и бота, тестам очереди это не нужно
    from mailpulse.worker.wiring import build_handlers

    settings = get_settings()
    engine = make_engine()
    sessionmaker = make_sessionmaker(engine)
    stale_after = timedelta(seconds=settings.worker_stale_job_after_s)
    last_reap = float("-inf")

    async with contextlib.AsyncExitStack() as stack:
        stack.push_async_callback(engine.dispose)
        handlers = {**BASE_HANDLERS, **await build_handlers(stack, settings, sessionmaker)}
        log.info("worker started, handlers: %s", sorted(handlers))

        while not stop.is_set():
            if time.monotonic() - last_reap > stale_after.total_seconds():
                async with sessionmaker.begin() as session:
                    if requeued := await queue.requeue_stale(session, stale_after):
                        log.warning("requeued %d stale jobs", requeued)
                last_reap = time.monotonic()
            if not await process_next(sessionmaker, handlers):
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=settings.worker_poll_interval_s)
    log.info("worker stopped")


def run() -> None:
    setup_logging(get_settings().log_level)

    async def main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await run_worker(stop)

    asyncio.run(main())
