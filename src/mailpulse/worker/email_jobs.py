"""Задачи воркера для графа: разбор нового письма и продолжение после нажатия кнопки."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from mailpulse.agent.graph import thread_config
from mailpulse.agent.ports import AgentDeps

log = logging.getLogger(__name__)

OwnerLookup = Callable[[int], Awaitable[int | None]]


class EmailJobs:
    def __init__(self, graph: CompiledStateGraph, deps: AgentDeps, owner_of: OwnerLookup) -> None:
        self._graph = graph
        self._deps = deps
        self._owner_of = owner_of

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {"process_email": self.process_email, "resume_email": self.resume_email}

    async def process_email(self, payload: dict[str, Any]) -> None:
        message_id = int(payload["message_id"])
        config = thread_config(message_id)
        snapshot = await self._graph.aget_state(config)
        if snapshot.values:
            if snapshot.next and not snapshot.interrupts:
                # Прошлый запуск упал посреди графа: продолжаем с последнего чекпоинта,
                # уже выполненные узлы (и отправленные сообщения) не повторяются
                log.info("message %s: continuing failed run from %s", message_id, snapshot.next)
                await self._graph.ainvoke(None, config, context=self._deps)
            return

        user_id = await self._owner_of(message_id)
        if user_id is None:
            log.warning("message %s: owner not found, skipping", message_id)
            return
        await self._graph.ainvoke(
            {"user_id": user_id, "message_id": message_id}, config, context=self._deps
        )

    async def resume_email(self, payload: dict[str, Any]) -> None:
        message_id = int(payload["message_id"])
        config = thread_config(message_id)
        snapshot = await self._graph.aget_state(config)
        if not snapshot.interrupts:
            # Двойное нажатие или кнопка старой карточки
            log.info("message %s: nothing to resume", message_id)
            return
        await self._graph.ainvoke(Command(resume=payload["answer"]), config, context=self._deps)
