"""Extractor на Postgres: текст вложений с кэшированием в БД и учётом стоимости vision."""

import asyncio
import logging
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.state import AttachmentText
from mailpulse.db import models as db
from mailpulse.documents.extract import extract_attachment
from mailpulse.documents.vision import ClaudeVision

log = logging.getLogger(__name__)


class PostgresExtractor:
    def __init__(
        self, sessionmaker: async_sessionmaker[AsyncSession], vision: ClaudeVision | None = None
    ) -> None:
        self._sessionmaker = sessionmaker
        self._vision = vision

    async def extract(self, message_id: int) -> list[AttachmentText]:
        async with self._sessionmaker() as session:
            attachments = list(
                (
                    await session.execute(
                        select(db.Attachment).where(db.Attachment.message_id == message_id)
                    )
                ).scalars()
            )
            user_id = await session.scalar(
                select(db.MailAccount.user_id)
                .join(db.Message, db.Message.account_id == db.MailAccount.id)
                .where(db.Message.id == message_id)
            )

        results: list[AttachmentText] = []
        for attachment in attachments:
            text, method = await self._text_for(attachment)
            if text:
                results.append(
                    {
                        "attachment_id": attachment.id,
                        "filename": attachment.filename,
                        "text": text,
                        "method": method,
                    }
                )

        if self._vision is not None:
            for meta in self._vision.drain():
                await self._record_vision_usage(user_id, meta)
        return results

    async def _text_for(self, attachment: db.Attachment) -> tuple[str, str]:
        # Уже извлекали (extraction_method проставлен) — не тратим vision повторно
        if attachment.extraction_method is not None:
            return attachment.extracted_text or "", attachment.extraction_method.value

        raw = await asyncio.to_thread(self._read_blob, attachment.storage_path)
        if raw is None:
            return "", db.ExtractionMethod.NONE.value
        extracted = await extract_attachment(
            raw, attachment.mime, attachment.filename, self._vision
        )

        async with self._sessionmaker.begin() as session:
            stored = await session.get(db.Attachment, attachment.id)
            stored.extracted_text = extracted.text
            stored.extraction_method = db.ExtractionMethod(extracted.method)
        log.info(
            "attachment %s (%s): %s, %d chars",
            attachment.id,
            attachment.filename,
            extracted.method,
            len(extracted.text),
        )
        return extracted.text, extracted.method

    @staticmethod
    def _read_blob(path: str) -> bytes | None:
        file = Path(path)
        return file.read_bytes() if file.exists() else None

    async def _record_vision_usage(self, user_id: int | None, meta) -> None:
        async with self._sessionmaker.begin() as session:
            session.add(
                db.LlmUsage(
                    user_id=user_id,
                    node="vision",
                    model=meta["model"],
                    input_tokens=meta["input_tokens"],
                    output_tokens=meta["output_tokens"],
                    cache_read_tokens=meta["cache_read_tokens"],
                    cache_write_tokens=meta["cache_write_tokens"],
                    cost_usd=Decimal(str(meta["cost_usd"])),
                )
            )
