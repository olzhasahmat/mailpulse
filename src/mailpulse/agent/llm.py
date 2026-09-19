"""Вызовы Claude: structured output, стоимость, метаданные для A/B и трейсинг в LangSmith."""

import time
from typing import Any

from anthropic import AsyncAnthropic

# Тот же преобразователь схемы, что внутри messages.parse() (DECISIONS.md, ADR-016).
# Модуль приватный: версия SDK закреплена в uv.lock, контракт проверяет tests/test_llm.py
from anthropic.lib._parse._transform import transform_schema
from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree
from pydantic import BaseModel, ValidationError

from mailpulse.agent.state import CallMeta

# $ за 1M токенов (вход, выход). Запись в кэш стоит 1.25× входа, чтение — 0.1× входа
PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.1


class LlmOutputError(RuntimeError):
    """Модель не дала валидный ответ: отказ, обрыв по max_tokens или ответ не по схеме."""


def make_client() -> AsyncAnthropic:
    # Без langsmith.wrappers.wrap_anthropic: в langsmith 0.12 он обращается к client.completions,
    # которого нет в anthropic 1.x. Вызовы трейсятся вручную в structured_call
    return AsyncAnthropic()


def usage_metadata(usage: Any) -> dict[str, Any]:
    """Токены в формате LangSmith: по ним дашборд считает стоимость трейса."""
    cache_read = usage.cache_read_input_tokens or 0
    cache_write = usage.cache_creation_input_tokens or 0
    input_tokens = usage.input_tokens + cache_read + cache_write
    return {
        "input_tokens": input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": input_tokens + usage.output_tokens,
        "input_token_details": {"cache_read": cache_read, "cache_creation": cache_write},
    }


def cost_usd(model: str, usage: Any) -> float:
    input_price, output_price = PRICES_PER_MTOK[model]
    cache_write = usage.cache_creation_input_tokens or 0
    cache_read = usage.cache_read_input_tokens or 0
    total = (
        usage.input_tokens * input_price
        + cache_write * input_price * CACHE_WRITE_MULTIPLIER
        + cache_read * input_price * CACHE_READ_MULTIPLIER
        + usage.output_tokens * output_price
    )
    return round(total / 1_000_000, 6)


async def content_call(
    client: AsyncAnthropic,
    *,
    model: str,
    prompt_version: str,
    system: str,
    content: list[dict[str, Any]],
    max_tokens: int,
    **params: Any,
) -> tuple[str, CallMeta]:
    """Вызов с произвольными блоками контента (текст + изображения/PDF), ответ — текст.

    Для vision-извлечения текста из вложений. Тот же ручной трейсинг, что и structured_call.
    """
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
        **params,
    }

    @traceable(
        run_type="llm",
        name=f"anthropic {model}",
        metadata={
            "ls_provider": "anthropic",
            "ls_model_name": model,
            "prompt_version": prompt_version,
        },
    )
    async def create(request: dict[str, Any]) -> Any:
        response = await client.messages.create(**request)
        if (run := get_current_run_tree()) is not None:
            run.metadata["usage_metadata"] = usage_metadata(response.usage)
        return response

    started = time.monotonic()
    response = await create(request)
    latency_ms = int((time.monotonic() - started) * 1000)

    if response.stop_reason in ("refusal", "max_tokens"):
        raise LlmOutputError(f"{model}: stop_reason={response.stop_reason}")
    text = "".join(block.text for block in response.content if block.type == "text")
    return text, _meta(model, prompt_version, response.usage, latency_ms)


def _meta(model: str, prompt_version: str, usage: Any, latency_ms: int) -> CallMeta:
    return {
        "model": model,
        "prompt_version": prompt_version,
        "latency_ms": latency_ms,
        "cost_usd": cost_usd(model, usage),
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_input_tokens or 0,
        "cache_write_tokens": usage.cache_creation_input_tokens or 0,
    }


async def structured_call[T: BaseModel](
    client: AsyncAnthropic,
    *,
    model: str,
    prompt_version: str,
    system: str,
    user: str,
    output: type[T],
    max_tokens: int,
    output_config: dict[str, Any] | None = None,
    **params: Any,
) -> tuple[T, CallMeta]:
    schema_format = {"type": "json_schema", "schema": transform_schema(output.model_json_schema())}
    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {**(output_config or {}), "format": schema_format},
        **params,
    }

    @traceable(
        run_type="llm",
        name=f"anthropic {model}",
        metadata={
            "ls_provider": "anthropic",
            "ls_model_name": model,
            "prompt_version": prompt_version,
        },
    )
    async def create(request: dict[str, Any]) -> Any:
        response = await client.messages.create(**request)
        if (run := get_current_run_tree()) is not None:
            run.metadata["usage_metadata"] = usage_metadata(response.usage)
        return response

    started = time.monotonic()
    response = await create(request)
    latency_ms = int((time.monotonic() - started) * 1000)

    if response.stop_reason in ("refusal", "max_tokens"):
        raise LlmOutputError(f"{model}: stop_reason={response.stop_reason}")
    text = next((block.text for block in response.content if block.type == "text"), None)
    if text is None:
        raise LlmOutputError(f"{model}: в ответе нет текста")
    try:
        parsed = output.model_validate_json(text)
    except ValidationError as exc:
        raise LlmOutputError(f"{model}: ответ не прошёл валидацию схемы") from exc

    return parsed, _meta(model, prompt_version, response.usage, latency_ms)
