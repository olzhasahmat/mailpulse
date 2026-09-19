"""Индексация письма: чанки → эмбеддинги → таблица chunks. Повторный вызов переиндексирует."""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.db.models import Chunk, MailAccount, Message
from mailpulse.rag.chunking import IndexableMessage, build_chunks
from mailpulse.rag.embeddings import Embedder


def indexable(message: Message) -> IndexableMessage:
    return IndexableMessage(
        from_addr=message.from_addr,
        from_name=message.from_name,
        to=[address["address"] for address in message.to_addrs],
        subject=message.subject,
        sent_at=message.sent_at,
        body=message.body_text,
        outgoing=message.outgoing,
    )


async def index_message(
    sessionmaker: async_sessionmaker[AsyncSession],
    embedder: Embedder,
    message_id: int,
    *,
    with_header: bool = True,
) -> int:
    """Возвращает число чанков. Эмбеддинги считаются вне транзакции: это самая долгая часть."""
    async with sessionmaker() as session:
        stmt = (
            select(Message, MailAccount.user_id)
            .join(MailAccount, MailAccount.id == Message.account_id)
            .where(Message.id == message_id)
        )
        row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return 0
    message, user_id = row

    texts = build_chunks(indexable(message), with_header=with_header)
    vectors = await embedder.embed_passages(texts) if texts else []

    async with sessionmaker.begin() as session:
        await session.execute(
            delete(Chunk).where(Chunk.message_id == message_id, Chunk.attachment_id.is_(None))
        )
        session.add_all(
            Chunk(
                user_id=user_id,
                message_id=message_id,
                chunk_index=index,
                content=text,
                embedding=vector,
                meta={"model": embedder.model_name},
            )
            for index, (text, vector) in enumerate(zip(texts, vectors, strict=True))
        )
    return len(texts)
