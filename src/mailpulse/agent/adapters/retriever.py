"""Retriever на Postgres: история текущего треда и похожие письма из гибридного поиска."""

import logging
import time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.state import EmailView, RetrievedChunk
from mailpulse.db.models import Message
from mailpulse.rag.chunking import message_header
from mailpulse.rag.embeddings import Embedder
from mailpulse.rag.indexer import indexable
from mailpulse.rag.search import hybrid_search

log = logging.getLogger(__name__)

THREAD_HISTORY_LIMIT = 3
THREAD_BODY_CHARS = 800
SIMILAR_LIMIT = 3
QUERY_BODY_CHARS = 1000
# Отсечение нерелевантных «похожих писем», подобрано make eval-retrieval на наборе
# с письмами-ловушками (ADR-021). Привязано к модели эмбеддингов: при её смене подбирать заново
SIMILAR_MAX_DISTANCE = 0.17
SIMILAR_RELATIVE_MARGIN = 0.03
SIMILAR_MIN_BODY_CHARS = 30


class PostgresRetriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        max_distance: float | None = SIMILAR_MAX_DISTANCE,
        relative_margin: float | None = SIMILAR_RELATIVE_MARGIN,
        min_body_chars: int = SIMILAR_MIN_BODY_CHARS,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._embedder = embedder
        self._max_distance = max_distance
        self._relative_margin = relative_margin
        self._min_body_chars = min_body_chars

    async def retrieve(self, user_id: int, email: EmailView) -> list[RetrievedChunk]:
        started = time.perf_counter()
        context = await self._thread_history(email)
        thread_done = time.perf_counter()

        query = f"{email['subject']}\n{email['body_text'][:QUERY_BODY_CHARS]}"
        vector = await self._embedder.embed_query(query)
        embed_done = time.perf_counter()
        async with self._sessionmaker() as session:
            hits = await hybrid_search(
                session,
                user_id=user_id,
                query_vector=vector,
                query_text=query,
                limit=SIMILAR_LIMIT,
                exclude_message_ids=[email["message_id"]],
                exclude_thread_id=email["thread_id"],
                max_distance=self._max_distance,
                relative_margin=self._relative_margin,
                min_body_chars=self._min_body_chars,
            )
        # Узел retrieve в трейсах медленнее локального замера: логируем, где уходит время
        log.info(
            "message %s: retrieve thread=%.0fms embed=%.0fms search=%.0fms, %d similar",
            email["message_id"],
            (thread_done - started) * 1000,
            (embed_done - thread_done) * 1000,
            (time.perf_counter() - embed_done) * 1000,
            len(hits),
        )
        context += [
            {
                "chunk_id": hit.chunk_id,
                "message_id": hit.message_id,
                "content": hit.content,
                "score": round(hit.score, 4),
                "source": "similar",
            }
            for hit in hits
        ]
        return context

    async def _thread_history(self, email: EmailView) -> list[RetrievedChunk]:
        if email["thread_id"] is None:
            return []
        stmt = (
            select(Message)
            .where(Message.thread_id == email["thread_id"], Message.id != email["message_id"])
            .order_by(Message.sent_at.desc().nulls_last(), Message.id.desc())
            .limit(THREAD_HISTORY_LIMIT)
        )
        async with self._sessionmaker() as session:
            messages = list((await session.execute(stmt)).scalars())
        return [
            {
                "chunk_id": None,
                "message_id": message.id,
                "content": f"{message_header(indexable(message))}\n\n"
                f"{message.body_text[:THREAD_BODY_CHARS]}",
                "score": 1.0,
                "source": "thread",
            }
            for message in reversed(messages)
        ]
