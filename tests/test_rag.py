import uuid

import pytest
from sqlalchemy import select

from mailpulse.agent.adapters.postgres import PostgresMailStore
from mailpulse.agent.adapters.retriever import PostgresRetriever
from mailpulse.db import models
from mailpulse.rag.embeddings import HashingEmbedder
from mailpulse.rag.indexer import index_message
from mailpulse.rag.search import hybrid_search, keyword_query

embedder = HashingEmbedder()


def test_keyword_query_puts_numbers_first_and_drops_short_words():
    query = keyword_query("Счёт №318 на оплату услуг")

    assert query.split(" | ")[0] == "318"
    assert "оплату" in query
    assert " на " not in f" {query} "


def test_keyword_query_for_text_without_words():
    assert keyword_query("?! -") is None


async def make_user(db, tg_user_id: int) -> tuple[int, int]:
    async with db.begin() as session:
        user = models.User(tg_user_id=tg_user_id, tg_chat_id=tg_user_id)
        session.add(user)
        await session.flush()
        account = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email=f"user{tg_user_id}@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
        )
        session.add(account)
        await session.flush()
        return user.id, account.id


async def add_message(
    db,
    account_id: int,
    subject: str,
    body: str,
    *,
    thread_id: int | None = None,
    index: bool = True,
) -> int:
    async with db.begin() as session:
        if thread_id is None:
            account = await session.get(models.MailAccount, account_id)
            thread = models.Thread(user_id=account.user_id, subject_norm=subject.casefold())
            session.add(thread)
            await session.flush()
            thread_id = thread.id
        message = models.Message(
            account_id=account_id,
            thread_id=thread_id,
            folder="INBOX",
            uid=1,
            message_id_hdr=f"<{uuid.uuid4()}@test>",
            from_addr="a.seitkali@partner.kz",
            subject=subject,
            body_text=body,
        )
        session.add(message)
        await session.flush()
        message_id = message.id
    if index:
        await index_message(db, embedder, message_id)
    return message_id


@pytest.fixture
async def corpus(db) -> dict[str, int]:
    user_id, account_id = await make_user(db, 1)
    _, other_account_id = await make_user(db, 2)
    ids = {
        "user_id": user_id,
        "account_id": account_id,
        "invoice": await add_message(
            db, account_id, "Счёт на оплату №318", "Направляем счёт на 180 000 тенге за сентябрь."
        ),
        "meeting": await add_message(
            db, account_id, "Встреча в пятницу", "Давайте созвонимся в пятницу обсудить поставку."
        ),
        "newsletter": await add_message(
            db, account_id, "Скидки недели", "Большие скидки на ноутбуки и телефоны."
        ),
        "foreign_invoice": await add_message(
            db,
            other_account_id,
            "Счёт на оплату №318",
            "Направляем счёт на 180 000 тенге за сентябрь.",
        ),
    }
    return ids


async def search(db, corpus, query: str, **options):
    async with db() as session:
        return await hybrid_search(
            session,
            user_id=corpus["user_id"],
            query_vector=await embedder.embed_query(query),
            query_text=query,
            **options,
        )


async def test_search_finds_relevant_message_only_among_own_mail(db, corpus):
    hits = await search(db, corpus, "счёт на оплату за сентябрь")

    assert hits[0].message_id == corpus["invoice"]
    assert corpus["foreign_invoice"] not in {hit.message_id for hit in hits}


async def test_exact_number_is_found_by_full_text(db, corpus):
    hits = await search(db, corpus, "318")

    assert hits[0].message_id == corpus["invoice"]


async def test_exclusions_and_one_hit_per_message(db, corpus):
    long_body = "\n\n".join(f"Абзац {n}: поставка оборудования и счёт. " * 40 for n in range(6))
    long_id = await add_message(db, corpus["account_id"], "Поставка оборудования", long_body)

    hits = await search(
        db, corpus, "поставка оборудования счёт", exclude_message_ids=[corpus["invoice"]]
    )

    message_ids = [hit.message_id for hit in hits]
    assert corpus["invoice"] not in message_ids
    assert message_ids.count(long_id) == 1


async def test_reindex_replaces_chunks(db, corpus):
    chunks_first = await index_message(db, embedder, corpus["invoice"])
    chunks_second = await index_message(db, embedder, corpus["invoice"])

    async with db() as session:
        stmt = select(models.Chunk).where(models.Chunk.message_id == corpus["invoice"])
        stored = list((await session.execute(stmt)).scalars())
    assert chunks_first == chunks_second == len(stored) == 1
    assert stored[0].content.startswith("От: a.seitkali@partner.kz")
    assert len(stored[0].embedding) == models.EMBEDDING_DIM


async def test_retriever_returns_thread_history_then_similar(db, corpus):
    account_id = corpus["account_id"]
    first = await add_message(
        db, account_id, "Поставка ноутбуков", "Когда будет поставка ноутбуков?"
    )
    async with db() as session:
        thread_id = (await session.get(models.Message, first)).thread_id
    second = await add_message(
        db, account_id, "Re: Поставка ноутбуков", "Поставка в среду.", thread_id=thread_id
    )
    current = await add_message(
        db,
        account_id,
        "Re: Поставка ноутбуков",
        "Подтвердите адрес для поставки ноутбуков.",
        thread_id=thread_id,
        index=False,
    )

    email = await PostgresMailStore(db).load_email(current)
    # Порог 0.18 подобран для e5; у хэширования другая шкала расстояний
    context = await PostgresRetriever(
        db, embedder, max_distance=None, relative_margin=None, min_body_chars=0
    ).retrieve(corpus["user_id"], email)

    thread = [item for item in context if item["source"] == "thread"]
    similar = [item for item in context if item["source"] == "similar"]
    assert [item["message_id"] for item in thread] == [first, second]
    assert current not in {item["message_id"] for item in context}
    assert {item["message_id"] for item in similar}.isdisjoint({first, second})
    assert corpus["newsletter"] in {item["message_id"] for item in similar}


async def test_text_mode_matches_exact_tokens_only(db, corpus):
    hits = await search(db, corpus, "318", mode="text")

    assert [hit.message_id for hit in hits] == [corpus["invoice"]]


async def test_max_distance_drops_unrelated_messages(db, corpus):
    unfiltered = await search(db, corpus, "счёт на оплату за сентябрь")
    filtered = await search(db, corpus, "счёт на оплату за сентябрь", max_distance=0.5)

    assert corpus["newsletter"] in {hit.message_id for hit in unfiltered}
    assert [hit.message_id for hit in filtered] == [corpus["invoice"]]
    assert filtered[0].distance < 0.5


def test_retriever_uses_rules_tuned_for_production_model():
    from mailpulse.agent.adapters import retriever as module

    tuned = PostgresRetriever(None, embedder)

    assert (tuned._max_distance, tuned._relative_margin, tuned._min_body_chars) == (
        module.SIMILAR_MAX_DISTANCE,
        module.SIMILAR_RELATIVE_MARGIN,
        module.SIMILAR_MIN_BODY_CHARS,
    )


async def test_min_body_chars_skips_contentless_messages(db, corpus):
    contentless = await add_message(db, corpus["account_id"], "Счёт на оплату", "Ок")

    hits = await search(db, corpus, "счёт на оплату", min_body_chars=10)

    message_ids = {hit.message_id for hit in hits}
    assert contentless not in message_ids
    assert corpus["invoice"] in message_ids


async def test_relative_margin_keeps_only_results_close_to_best(db, corpus):
    hits = await search(db, corpus, "счёт на оплату за сентябрь", relative_margin=0.05)

    assert [hit.message_id for hit in hits] == [corpus["invoice"]]
