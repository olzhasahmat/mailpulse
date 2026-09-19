"""Оценка поиска: recall@5 и MRR на синтетическом корпусе.

Сравнивает режимы поиска (векторный, полнотекстовый, гибрид) на чанках с заголовком и без,
а для гибрида подбирает порог релевантности по запросам, на которые в корпусе ответа нет.
Работает в отдельной базе <имя>_eval, рабочие данные не трогает.

    make eval-retrieval                   # multilingual-e5-large, как в работе
    make eval-retrieval EMBEDDER=hashing  # быстрый прогон без модели
"""

import argparse
import asyncio
import json
import time
from datetime import datetime
from pathlib import Path
from statistics import mean, median

from sqlalchemy import text

from mailpulse.agent.adapters.retriever import (
    SIMILAR_MAX_DISTANCE,
    SIMILAR_MIN_BODY_CHARS,
    SIMILAR_RELATIVE_MARGIN,
)
from mailpulse.config import get_settings
from mailpulse.db import models
from mailpulse.db.base import Base
from mailpulse.db.provision import ensure_database
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.rag.embeddings import FastEmbedEmbedder, HashingEmbedder
from mailpulse.rag.indexer import index_message
from mailpulse.rag.search import hybrid_search

HERE = Path(__file__).resolve().parent
K = 5
CONTEXT_LIMIT = 3  # столько похожих писем берёт Retriever
MODES = ("vector", "text", "hybrid")
THRESHOLD_STEPS = 20
RULE_LIMIT = 10  # для правил отсечения берём больше кандидатов: фильтр идёт до обрезки до 3
MARGINS = [None, 0.02, 0.03, 0.04, 0.06]
MIN_BODY_CHARS = [0, 30, 60]
USER_EMAIL = "you@gmail.com"

Runs = dict[str, list[tuple[str, float]]]


def read_jsonl(path: Path) -> list[dict]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


async def load_corpus(sessionmaker, docs: list[dict]) -> tuple[int, dict[str, int]]:
    tables = ", ".join(f'"{table.name}"' for table in Base.metadata.sorted_tables)
    async with sessionmaker.begin() as session:
        await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        user = models.User(tg_user_id=0, tg_chat_id=0)
        session.add(user)
        await session.flush()
        account = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email=USER_EMAIL,
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"eval",
            imap_host="imap.invalid",
            smtp_host="smtp.invalid",
        )
        session.add(account)
        await session.flush()

        threads: dict[str, int] = {}
        ids: dict[str, int] = {}
        for uid, doc in enumerate(docs, start=1):
            if doc["thread"] not in threads:
                thread = models.Thread(user_id=user.id, subject_norm=doc["thread"])
                session.add(thread)
                await session.flush()
                threads[doc["thread"]] = thread.id
            outgoing = doc.get("outgoing", False)
            message = models.Message(
                account_id=account.id,
                thread_id=threads[doc["thread"]],
                folder="Sent" if outgoing else "INBOX",
                uid=uid,
                message_id_hdr=f"<{doc['key']}@eval.mailpulse>",
                from_addr=USER_EMAIL if outgoing else doc["from_addr"],
                from_name=None if outgoing else doc.get("from_name"),
                to_addrs=[{"name": "", "address": a} for a in doc.get("to", [USER_EMAIL])],
                subject=doc["subject"],
                sent_at=datetime.fromisoformat(doc["sent_at"]),
                body_text=doc["body"],
                outgoing=outgoing,
            )
            session.add(message)
            await session.flush()
            ids[doc["key"]] = message.id
        return user.id, ids


async def run_queries(sessionmaker, user_id, queries, vectors, key_of, mode, limit=K) -> Runs:
    runs: Runs = {}
    for query in queries:
        async with sessionmaker() as session:
            hits = await hybrid_search(
                session,
                user_id=user_id,
                query_vector=vectors[query["id"]],
                query_text=query["text"],
                limit=limit,
                mode=mode,
            )
        runs[query["id"]] = [(key_of[hit.message_id], round(hit.distance, 4)) for hit in hits]
    return runs


def recall(ranked: list[str], relevant: list[str], k: int) -> float:
    return len(set(ranked[:k]) & set(relevant)) / len(relevant)


def reciprocal_rank(ranked: list[str], relevant: list[str]) -> float:
    return next((1 / position for position, key in enumerate(ranked, 1) if key in relevant), 0.0)


def ranked_keys(runs: Runs, query: dict, max_distance: float | None = None) -> list[str]:
    return [
        key
        for key, distance in runs[query["id"]]
        if max_distance is None or distance <= max_distance
    ]


def summarize(queries: list[dict], runs: Runs) -> dict[str, float]:
    positives = [q for q in queries if q["relevant"]]
    summary = {
        "recall@5": mean(recall(ranked_keys(runs, q), q["relevant"], K) for q in positives),
        "mrr": mean(reciprocal_rank(ranked_keys(runs, q), q["relevant"]) for q in positives),
    }
    for kind in ("search", "email"):
        subset = [q for q in positives if q["kind"] == kind]
        summary[f"recall@5 {kind}"] = mean(
            recall(ranked_keys(runs, q), q["relevant"], K) for q in subset
        )
    return summary


def threshold_grid(runs: Runs) -> list[float]:
    """Пороги между наименьшим и наибольшим встреченным расстоянием.

    Шкала расстояний у моделей разная (e5 и хэширование отличаются в разы), поэтому
    сетка строится по фактическим результатам, а не задаётся заранее.
    """
    distances = [distance for hits in runs.values() for _, distance in hits]
    low, high = min(distances), max(distances)
    step = (high - low) / THRESHOLD_STEPS
    return [round(low + step * i, 3) for i in range(THRESHOLD_STEPS + 1)]


def apply_rules(
    hits: list[tuple[str, float]],
    body_chars: dict[str, int],
    *,
    max_distance: float | None,
    margin: float | None,
    min_body: int,
) -> list[str]:
    """Тот же порядок, что в hybrid_search: длина текста → отставание от лучшего → порог."""
    kept = [(key, distance) for key, distance in hits if body_chars[key] >= min_body]
    if margin is not None and kept:
        best = min(distance for _, distance in kept)
        kept = [(key, distance) for key, distance in kept if distance <= best + margin]
    if max_distance is not None:
        kept = [(key, distance) for key, distance in kept if distance <= max_distance]
    return [key for key, _ in kept][:CONTEXT_LIMIT]


def evaluate_rule(queries, runs, body_chars, **rule) -> dict[str, float]:
    positives = [q for q in queries if q["relevant"]]
    negatives = [q for q in queries if not q["relevant"]]
    kept = {q["id"]: apply_rules(runs[q["id"]], body_chars, **rule) for q in queries}
    recall3 = mean(recall(kept[q["id"]], q["relevant"], CONTEXT_LIMIT) for q in positives)
    noise = mean(sum(key not in q["relevant"] for key in kept[q["id"]]) for q in positives)
    false_context = mean(float(bool(kept[q["id"]])) for q in negatives)
    return {
        **rule,
        "recall@3": recall3,
        "noise": noise,
        "false_context": false_context,
        # Пропущенное релевантное и лишнее письмо в контексте весят одинаково
        "utility": recall3 - noise / CONTEXT_LIMIT - false_context,
    }


def rules_table(queries, runs, body_chars) -> list[dict[str, float]]:
    thresholds = [None, *threshold_grid(runs)]
    return [
        evaluate_rule(queries, runs, body_chars, max_distance=t, margin=m, min_body=b)
        for t in thresholds
        for m in MARGINS
        for b in MIN_BODY_CHARS
    ]


def distance_stats(queries: list[dict], runs: Runs) -> dict[str, list[float]]:
    first_relevant = [
        next((d for key, d in runs[q["id"]] if key in q["relevant"]), None)
        for q in queries
        if q["relevant"]
    ]
    first_hit_of_negative = [
        runs[q["id"]][0][1] for q in queries if not q["relevant"] and runs[q["id"]]
    ]
    return {
        "positives": [d for d in first_relevant if d is not None],
        "negatives": first_hit_of_negative,
    }


def render_markdown(report: dict, queries: list[dict]) -> str:
    positives = sum(1 for q in queries if q["relevant"])
    lines = [
        f"# Оценка поиска: {report['embedder']}",
        "",
        f"Корпус: {report['corpus']} писем. Запросов: {len(queries)} — {positives} с ответом "
        f"и {len(queries) - positives} без ответа в корпусе. "
        "Сгенерировано `evals/retrieval/run.py`.",
        "",
        "## Режимы поиска",
        "",
        "| Чанки | Режим | recall@5 | MRR | recall@5 (поиск) | recall@5 (письмо) |",
        "|---|---|---|---|---|---|",
    ]
    for name, config in report["configs"].items():
        header, mode = name.split("/")
        s = config["summary"]
        header_label = "с заголовком" if header == "header" else "без заголовка"
        lines.append(
            f"| {header_label} | {mode} | {s['recall@5']:.2f} | {s['mrr']:.2f} "
            f"| {s['recall@5 search']:.2f} | {s['recall@5 email']:.2f} |"
        )

    stats = report["distances"]
    lines += [
        "",
        "## Расстояние до результата (гибрид, чанки с заголовком)",
        "",
        "| | min | медиана | max |",
        "|---|---|---|---|",
    ]
    for label, values in (
        ("первое релевантное письмо", stats["positives"]),
        ("первый результат запроса без ответа", stats["negatives"]),
    ):
        if values:
            lines.append(
                f"| {label} | {min(values):.3f} | {median(values):.3f} | {max(values):.3f} |"
            )

    lines += [
        "",
        "## Правила отсечения контекста (гибрид, чанки с заголовком)",
        "",
        f"Для каждого запроса берутся {RULE_LIMIT} кандидатов, к ним применяются правила, "
        f"в контекст идут первые {CONTEXT_LIMIT}. recall@3 — доля релевантного в контексте. "
        "Шум — сколько нерелевантных писем в контексте запроса, у которого ответ есть "
        "(оценка пессимистичная: неразмеченные, но связанные письма тоже считаются шумом). "
        "Ложный контекст — доля запросов без ответа, по которым что-то попало в контекст. "
        "Полезность = recall@3 − шум / 3 − ложный контекст.",
        "",
    ]

    def rule_row(row: dict) -> str:
        threshold = "—" if row["max_distance"] is None else f"{row['max_distance']:.3f}"
        margin = "—" if row["margin"] is None else f"{row['margin']:.2f}"
        return (
            f"| {threshold} | {margin} | {row['min_body']} | {row['recall@3']:.2f} "
            f"| {row['noise']:.2f} | {row['false_context']:.0%} | {row['utility']:.2f} |"
        )

    header = [
        "| max distance | отставание | мин. длина текста | recall@3 | шум "
        "| ложный контекст | полезность |",
        "|---|---|---|---|---|---|---|",
    ]
    rules = report["rules"]
    baseline = next(
        r for r in rules if r["max_distance"] is None and r["margin"] is None and r["min_body"] == 0
    )
    lines += ["### Без правил", "", *header, rule_row(baseline), ""]
    lines += [
        "### Правило в работе (agent/adapters/retriever.py)",
        "",
        *header,
        rule_row(report["production_rule"]),
        "",
    ]
    absolute = [
        r
        for r in rules
        if r["margin"] is None and r["min_body"] == 0 and r["max_distance"] is not None
    ]
    lines += ["### Только порог расстояния", "", *header, *map(rule_row, absolute), ""]
    top = sorted(rules, key=lambda r: r["utility"], reverse=True)[:12]
    lines += ["### Лучшие сочетания по полезности", "", *header, *map(rule_row, top)]

    runs = report["configs"]["header/hybrid"]["runs"]
    misses = [
        q for q in queries if q["relevant"] and recall(ranked_keys(runs, q), q["relevant"], K) < 1
    ]
    lines += ["", "## Промахи гибрида (чанки с заголовком)", ""]
    if not misses:
        lines.append("Нет: все релевантные письма в топ-5.")
    else:
        lines += ["| Запрос | Ожидалось | Найдено (distance) |", "|---|---|---|"]
        for q in misses:
            found = ", ".join(f"{key} ({distance:.3f})" for key, distance in runs[q["id"]])
            query_text = q["text"].split("\n")[0]
            lines.append(f"| {q['id']}: {query_text} | {', '.join(q['relevant'])} | {found} |")
    return "\n".join(lines) + "\n"


async def main(embedder_name: str) -> None:
    settings = get_settings()
    url = ensure_database(settings.database_url, "eval")
    engine = make_engine(url)
    sessionmaker = make_sessionmaker(engine)
    docs = read_jsonl(HERE / "corpus.jsonl")
    queries = read_jsonl(HERE / "queries.jsonl")

    if embedder_name == "hashing":
        embedder = HashingEmbedder()
    else:
        embedder = await asyncio.to_thread(
            FastEmbedEmbedder.by_name, settings.embedding_model, str(settings.embedding_cache_dir)
        )

    user_id, ids = await load_corpus(sessionmaker, docs)
    key_of = {message_id: key for key, message_id in ids.items()}
    vectors = {query["id"]: await embedder.embed_query(query["text"]) for query in queries}

    report: dict = {"embedder": embedder.model_name, "corpus": len(docs), "configs": {}}
    for with_header in (True, False):
        started = time.monotonic()
        for message_id in ids.values():
            await index_message(sessionmaker, embedder, message_id, with_header=with_header)
        index_seconds = round(time.monotonic() - started, 1)
        if with_header:
            rule_runs = await run_queries(
                sessionmaker, user_id, queries, vectors, key_of, "hybrid", limit=RULE_LIMIT
            )
        for mode in MODES:
            runs = await run_queries(sessionmaker, user_id, queries, vectors, key_of, mode)
            name = f"{'header' if with_header else 'no-header'}/{mode}"
            report["configs"][name] = {
                "summary": summarize(queries, runs),
                "runs": runs,
                "index_seconds": index_seconds,
            }
    await engine.dispose()

    best = report["configs"]["header/hybrid"]["runs"]
    body_chars = {doc["key"]: len(doc["body"]) for doc in docs}
    report["rules"] = rules_table(queries, rule_runs, body_chars)
    report["production_rule"] = evaluate_rule(
        queries,
        rule_runs,
        body_chars,
        max_distance=SIMILAR_MAX_DISTANCE,
        margin=SIMILAR_RELATIVE_MARGIN,
        min_body=SIMILAR_MIN_BODY_CHARS,
    )
    report["distances"] = distance_stats(queries, best)

    (HERE / f"results-{embedder_name}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = render_markdown(report, queries)
    (HERE / f"results-{embedder_name}.md").write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--embedder", choices=["e5", "hashing"], default="e5")
    asyncio.run(main(parser.parse_args().embedder))
