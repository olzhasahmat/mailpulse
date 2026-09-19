import hashlib

import pytest
from sqlalchemy import func, select

from mailpulse.agent.adapters.extractor import PostgresExtractor
from mailpulse.db import models

pytestmark = pytest.mark.db


class FakeVision:
    def __init__(self) -> None:
        self.calls = 0
        self.metas: list[dict] = []

    async def __call__(self, raw: bytes, mime: str, filename: str) -> str:
        self.calls += 1
        self.metas.append(
            {
                "model": "claude-sonnet-5",
                "prompt_version": "vision-v1",
                "latency_ms": 10,
                "cost_usd": 0.004,
                "input_tokens": 1500,
                "output_tokens": 60,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
            }
        )
        return "СЧЁТ №507 на 320 000 тенге"

    def drain(self) -> list[dict]:
        metas, self.metas = self.metas, []
        return metas


async def make_message_with_image(db, tmp_path) -> int:
    payload = b"\xff\xd8fake-jpeg-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    blob = tmp_path / digest
    blob.write_bytes(payload)
    async with db.begin() as session:
        user = models.User(tg_user_id=1, tg_chat_id=1)
        session.add(user)
        await session.flush()
        account = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email="you@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
        )
        session.add(account)
        await session.flush()
        message = models.Message(
            account_id=account.id,
            folder="INBOX",
            uid=1,
            message_id_hdr="<m@x>",
            from_addr="buh@partner.kz",
            subject="Счёт",
            has_attachments=True,
        )
        session.add(message)
        await session.flush()
        session.add(
            models.Attachment(
                message_id=message.id,
                filename="schet.jpg",
                mime="image/jpeg",
                size=len(payload),
                sha256=digest,
                storage_path=str(blob),
            )
        )
        return message.id


async def test_vision_extraction_is_cached_and_billed(db, tmp_path):
    message_id = await make_message_with_image(db, tmp_path)
    vision = FakeVision()
    extractor = PostgresExtractor(db, vision)

    first = await extractor.extract(message_id)
    second = await extractor.extract(message_id)

    assert first == second
    assert first[0]["method"] == "vision"
    assert "320 000" in first[0]["text"]
    # Второй раз vision не вызывается — текст взят из БД
    assert vision.calls == 1
    async with db() as session:
        attachment = (await session.execute(select(models.Attachment))).scalar_one()
        usage = await session.scalar(
            select(func.count())
            .select_from(models.LlmUsage)
            .where(models.LlmUsage.node == "vision")
        )
    assert attachment.extraction_method == models.ExtractionMethod.VISION
    assert usage == 1  # оплачен один вызов, несмотря на два extract


async def test_message_without_attachments_returns_empty(db, tmp_path):
    async with db.begin() as session:
        user = models.User(tg_user_id=2, tg_chat_id=2)
        session.add(user)
        await session.flush()
        account = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email="a@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="i",
            smtp_host="s",
        )
        session.add(account)
        await session.flush()
        message = models.Message(
            account_id=account.id,
            folder="INBOX",
            uid=1,
            message_id_hdr="<n@x>",
            from_addr="x@y.z",
            subject="Без вложений",
        )
        session.add(message)
        await session.flush()
        message_id = message.id

    assert await PostgresExtractor(db, FakeVision()).extract(message_id) == []
