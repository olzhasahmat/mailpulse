"""Сборка обработчиков воркера с реальными адаптерами."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.adapters.claude import ClaudeClassifier, ClaudeSummarizer
from mailpulse.agent.adapters.drafter import ClaudeDrafter
from mailpulse.agent.adapters.extractor import PostgresExtractor
from mailpulse.agent.adapters.guard import HeuristicGuard
from mailpulse.agent.adapters.outbox import SmtpOutbox
from mailpulse.agent.adapters.postgres import PostgresMailStore, owner_of_message
from mailpulse.agent.adapters.retriever import PostgresRetriever
from mailpulse.agent.graph import build_graph
from mailpulse.agent.llm import make_client
from mailpulse.agent.ports import AgentDeps
from mailpulse.agent.skills import SkillRegistry
from mailpulse.bot.notifier import TelegramNotifier, create_bot
from mailpulse.config import Settings
from mailpulse.documents.vision import ClaudeVision
from mailpulse.rag.embeddings import FastEmbedEmbedder
from mailpulse.security import SecretsCipher
from mailpulse.worker.email_jobs import EmailJobs
from mailpulse.worker.index_jobs import IndexJobs

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


async def build_handlers(
    stack: AsyncExitStack, settings: Settings, sessionmaker: async_sessionmaker[AsyncSession]
) -> dict[str, Handler]:
    """Индексация работает всегда; разбор писем — только когда заданы ключи."""
    log.info("loading embedding model %s", settings.embedding_model)
    embedder = await asyncio.to_thread(
        FastEmbedEmbedder.by_name, settings.embedding_model, str(settings.embedding_cache_dir)
    )
    handlers: dict[str, Handler] = IndexJobs(sessionmaker, embedder).handlers()

    missing = [
        name
        for name, present in (
            ("ANTHROPIC_API_KEY", bool(os.environ.get("ANTHROPIC_API_KEY"))),
            ("TELEGRAM_BOT_TOKEN", settings.telegram_bot_token is not None),
        )
        if not present
    ]
    if missing:
        log.warning("разбор писем выключен, не заданы: %s", ", ".join(missing))
        return handlers

    if settings.secrets_key is None:
        log.warning("разбор писем выключен: не задан SECRETS_KEY (make secret-key)")
        return handlers
    cipher = SecretsCipher.from_settings(settings)
    checkpointer = await stack.enter_async_context(
        AsyncPostgresSaver.from_conn_string(settings.psycopg_dsn)
    )
    skills = SkillRegistry.load()
    bot = create_bot(settings.telegram_bot_token.get_secret_value())
    stack.push_async_callback(bot.session.close)
    client = make_client()
    stack.push_async_callback(client.close)

    deps = AgentDeps(
        store=PostgresMailStore(sessionmaker),
        extractor=PostgresExtractor(sessionmaker, ClaudeVision(client)),
        guard=HeuristicGuard(),
        retriever=PostgresRetriever(sessionmaker, embedder),
        classifier=ClaudeClassifier(client, skills),
        summarizer=ClaudeSummarizer(client),
        drafter=ClaudeDrafter(client, skills),
        notifier=TelegramNotifier(bot, sessionmaker),
        outbox=SmtpOutbox(sessionmaker, cipher),
    )

    async def owner_of(message_id: int) -> int | None:
        async with sessionmaker() as session:
            owner = await owner_of_message(session, message_id)
        return owner.id if owner else None

    handlers.update(EmailJobs(build_graph(checkpointer), deps, owner_of).handlers())
    return handlers
