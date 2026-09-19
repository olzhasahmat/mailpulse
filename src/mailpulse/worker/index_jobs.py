"""Задача воркера: индексация письма для RAG."""

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.rag.embeddings import Embedder
from mailpulse.rag.indexer import index_message

log = logging.getLogger(__name__)


class IndexJobs:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], embedder: Embedder) -> None:
        self._sessionmaker = sessionmaker
        self._embedder = embedder

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], Awaitable[None]]]:
        return {"index_email": self.index_email}

    async def index_email(self, payload: dict[str, Any]) -> None:
        message_id = int(payload["message_id"])
        chunks = await index_message(self._sessionmaker, self._embedder, message_id)
        log.info("message %s: indexed %d chunks", message_id, chunks)
