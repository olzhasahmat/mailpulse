"""Эмбеддинги: порт и локальная реализация на fastembed (ONNX, без PyTorch)."""

import asyncio
import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from mailpulse.db.models import EMBEDDING_DIM


class Embedder(Protocol):
    model_name: str

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]: ...
    async def embed_query(self, text: str) -> list[float]: ...


@dataclass(frozen=True)
class ModelSpec:
    name: str
    dim: int
    query_prefix: str = ""
    passage_prefix: str = ""


# e5 обучена с префиксами: без них качество поиска падает, а fastembed их сам не добавляет
E5_LARGE = ModelSpec("intfloat/multilingual-e5-large", 1024, "query: ", "passage: ")
KNOWN_MODELS = {spec.name: spec for spec in (E5_LARGE,)}


class FastEmbedEmbedder:
    def __init__(self, spec: ModelSpec, cache_dir: str | None = None, batch_size: int = 16) -> None:
        if spec.dim != EMBEDDING_DIM:
            raise ValueError(
                f"{spec.name} даёт {spec.dim} измерений, а chunks.embedding — {EMBEDDING_DIM}"
            )
        from fastembed import TextEmbedding  # тяжёлый импорт: onnxruntime

        self.model_name = spec.name
        self._spec = spec
        self._batch_size = batch_size
        self._model = TextEmbedding(spec.name, cache_dir=cache_dir)
        # Одна ONNX-сессия на процесс: не запускаем её из нескольких потоков одновременно
        self._lock = asyncio.Lock()

    @classmethod
    def by_name(cls, name: str, cache_dir: str | None = None) -> "FastEmbedEmbedder":
        if name not in KNOWN_MODELS:
            raise ValueError(
                f"неизвестная модель эмбеддингов {name!r}, есть: {sorted(KNOWN_MODELS)}"
            )
        return cls(KNOWN_MODELS[name], cache_dir)

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        prefixed = [self._spec.passage_prefix + text for text in texts]
        async with self._lock:
            vectors = await asyncio.to_thread(
                lambda: list(self._model.embed(prefixed, batch_size=self._batch_size))
            )
        return [vector.tolist() for vector in vectors]

    async def embed_query(self, text: str) -> list[float]:
        async with self._lock:
            [vector] = await asyncio.to_thread(
                lambda: list(self._model.embed([self._spec.query_prefix + text]))
            )
        return vector.tolist()


class HashingEmbedder:
    """Детерминированные «эмбеддинги» по словам без модели: базовая линия в оценках и тестах."""

    model_name = "hashing"

    async def embed_passages(self, texts: Sequence[str]) -> list[list[float]]:
        return [hashing_vector(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return hashing_vector(text)


def hashing_vector(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIM
    for word in re.findall(r"\w+", text.casefold()):
        vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % EMBEDDING_DIM] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]
