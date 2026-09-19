"""Гибридный поиск по письмам пользователя: векторный (pgvector) и полнотекстовый, слияние RRF."""

import re
from collections import defaultdict
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from mailpulse.db.models import Chunk, Message

RRF_K = 60
CANDIDATES = 30
MAX_QUERY_TERMS = 16
TOKEN_RE = re.compile(r"[0-9a-zа-яёәғқңөұүһі]+", re.I)

# vector и text — варианты для сравнения в evals/retrieval, в работе используется hybrid
SearchMode = Literal["hybrid", "vector", "text"]


@dataclass(frozen=True)
class SearchHit:
    chunk_id: int
    message_id: int
    thread_id: int | None
    content: str
    score: float
    distance: float  # косинусное расстояние лучшего чанка до запроса


def keyword_query(source: str) -> str | None:
    """OR-запрос для полнотекстового поиска.

    Текст письма длинный, и AND по всем словам почти ничего не находит. Первыми идут токены
    с цифрами (номера счетов, суммы), затем самые длинные слова.
    """
    tokens = {
        token
        for token in TOKEN_RE.findall(source.casefold())
        if len(token) >= 3 or (token.isdigit() and len(token) >= 2)
    }
    ranked = sorted(tokens, key=lambda t: (not any(c.isdigit() for c in t), -len(t), t))
    return " | ".join(ranked[:MAX_QUERY_TERMS]) or None


async def hybrid_search(
    session: AsyncSession,
    *,
    user_id: int,
    query_vector: list[float],
    query_text: str,
    limit: int = 5,
    exclude_message_ids: Collection[int] = (),
    exclude_thread_id: int | None = None,
    mode: SearchMode = "hybrid",
    max_distance: float | None = None,
    relative_margin: float | None = None,
    min_body_chars: int = 0,
) -> list[SearchHit]:
    """До limit писем, по одному лучшему чанку на письмо.

    Поиск всегда находит «ближайшее», даже когда ничего релевантного в архиве нет, поэтому
    есть три правила отсечения, применяемые по порядку:
    - min_body_chars — письма почти без текста («Ок», «Проверка связи») близки к любому запросу;
    - relative_margin — результат хуже лучшего больше чем на margin отбрасывается;
    - max_distance — абсолютный порог косинусного расстояния.
    """
    filters = [Chunk.user_id == user_id]
    if exclude_message_ids:
        filters.append(Chunk.message_id.not_in(list(exclude_message_ids)))
    if exclude_thread_id is not None:
        filters.append(or_(Message.thread_id.is_(None), Message.thread_id != exclude_thread_id))

    distance = Chunk.embedding.cosine_distance(query_vector)
    ranked_lists: list[list[int]] = []
    if mode in ("hybrid", "vector"):
        # HNSW сначала находит ближайших, потом фильтрует по user_id; без итеративного скана
        # у пользователя с небольшим архивом кандидатов могло бы не набраться
        await session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        vector_stmt = (
            select(Chunk.id)
            .join(Message, Message.id == Chunk.message_id)
            .where(*filters, Chunk.embedding.is_not(None))
            .order_by(distance)
            .limit(CANDIDATES)
        )
        ranked_lists.append(list((await session.execute(vector_stmt)).scalars()))

    if mode in ("hybrid", "text") and (keywords := keyword_query(query_text)):
        tsquery = func.to_tsquery("simple", keywords)
        text_stmt = (
            select(Chunk.id)
            .join(Message, Message.id == Chunk.message_id)
            .where(*filters, Chunk.tsv.op("@@")(tsquery))
            .order_by(func.ts_rank_cd(Chunk.tsv, tsquery).desc())
            .limit(CANDIDATES)
        )
        ranked_lists.append(list((await session.execute(text_stmt)).scalars()))

    scores: dict[int, float] = defaultdict(float)
    for ranked in ranked_lists:
        for position, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] += 1 / (RRF_K + position)
    if not scores:
        return []

    rows_stmt = (
        select(
            Chunk.id,
            Chunk.message_id,
            Chunk.content,
            Message.thread_id,
            distance.label("distance"),
            func.length(Message.body_text).label("body_chars"),
        )
        .join(Message, Message.id == Chunk.message_id)
        .where(Chunk.id.in_(list(scores)))
    )
    rows = {row.id: row for row in await session.execute(rows_stmt)}

    # Лучший по RRF чанк каждого письма, затем правила отсечения
    candidates = []
    seen_messages: set[int] = set()
    for chunk_id in sorted(scores, key=scores.__getitem__, reverse=True):
        row = rows[chunk_id]
        if row.message_id in seen_messages:
            continue
        seen_messages.add(row.message_id)
        if row.body_chars >= min_body_chars:
            candidates.append(row)
    if relative_margin is not None and candidates:
        best = min(row.distance for row in candidates)
        candidates = [row for row in candidates if row.distance <= best + relative_margin]
    if max_distance is not None:
        candidates = [row for row in candidates if row.distance <= max_distance]

    return [
        SearchHit(
            chunk_id=row.id,
            message_id=row.message_id,
            thread_id=row.thread_id,
            content=row.content,
            score=scores[row.id],
            distance=float(row.distance),
        )
        for row in candidates[:limit]
    ]
