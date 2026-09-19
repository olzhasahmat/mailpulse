from datetime import UTC, datetime

from mailpulse.rag.chunking import (
    MAX_BODY_CHARS,
    IndexableMessage,
    build_chunks,
    message_header,
    split_body,
)


def make_message(body: str = "Оплатите счёт до пятницы.", **overrides) -> IndexableMessage:
    values = {
        "from_addr": "a.seitkali@partner.kz",
        "from_name": "Айгерим Сейткали",
        "to": ["you@gmail.com"],
        "subject": "Счёт на оплату №318",
        "sent_at": datetime(2026, 9, 12, 9, 0, tzinfo=UTC),
        "body": body,
        "outgoing": False,
    }
    return IndexableMessage(**{**values, **overrides})


def test_short_email_is_one_chunk_with_header():
    [chunk] = build_chunks(make_message())

    assert chunk == (
        "От: Айгерим Сейткали <a.seitkali@partner.kz>\n"
        "Кому: you@gmail.com\n"
        "Тема: Счёт на оплату №318\n"
        "Дата: 2026-09-12\n\n"
        "Оплатите счёт до пятницы."
    )


def test_outgoing_message_is_marked():
    header = message_header(make_message(outgoing=True, from_name=None))

    assert header.startswith("От: a.seitkali@partner.kz (пользователь)")


def test_chunks_without_header_for_ab_experiment():
    assert build_chunks(make_message(), with_header=False) == ["Оплатите счёт до пятницы."]


def test_empty_body_keeps_header_so_subject_is_searchable():
    [chunk] = build_chunks(make_message(body=""))

    assert "Тема: Счёт на оплату №318" in chunk


def test_long_body_is_split_with_overlap_and_nothing_lost():
    paragraphs = [f"Абзац {n}. " + "Текст про поставку и оплату. " * 20 for n in range(8)]
    body = "\n\n".join(paragraphs)

    chunks = split_body(body)

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_BODY_CHARS for chunk in chunks)
    for n in range(8):
        assert any(f"Абзац {n}." in chunk for chunk in chunks)
    # Начало второго чанка повторяет конец первого
    first_words = chunks[1].split()[:3]
    assert " ".join(first_words) in chunks[0]


def test_giant_sentence_is_hard_split_by_words():
    body = " ".join(["слово"] * 1000)

    chunks = split_body(body)

    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_BODY_CHARS for chunk in chunks)
    assert sum(chunk.count("слово") for chunk in chunks) >= 1000
