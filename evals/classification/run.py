"""Оценка классификации на golden.jsonl: accuracy, macro-F1, recall для важности ≥2,
извлечение сущностей, детект suspicious. Сравнивает конфигурации (A/B) по модели и температуре.

    make eval-classify                 # все конфигурации (тратит деньги на API)
    make eval-classify SPLIT=dev       # только dev-часть
    make eval-classify CONFIGS=prod    # одна конфигурация

Каждый вызов классификатора трейсится в LangSmith (проект mailpulse).
"""

import argparse
import asyncio
import json
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from mailpulse.agent.adapters.claude import ClaudeClassifier
from mailpulse.agent.llm import make_client
from mailpulse.agent.skills import SkillRegistry

HERE = Path(__file__).resolve().parent
TODAY = date(2026, 9, 12)  # фиксируем «сегодня»: относительные даты детерминированы
CONCURRENCY = 5
IMPORTANCE_LEVELS = (0, 1, 2, 3)


@dataclass(frozen=True)
class Config:
    name: str
    model: str
    sampling: dict
    label: str


CONFIGS = {
    "prod": Config("prod", "claude-haiku-4-5", {"extra_body": {"temperature": 0.0}}, "Haiku, t=0"),
    "haiku-t05": Config(
        "haiku-t05", "claude-haiku-4-5", {"extra_body": {"temperature": 0.5}}, "Haiku, t=0.5"
    ),
    "haiku-t10": Config(
        "haiku-t10", "claude-haiku-4-5", {"extra_body": {"temperature": 1.0}}, "Haiku, t=1.0"
    ),
    "sonnet": Config(
        "sonnet", "claude-sonnet-5", {"output_config": {"effort": "low"}}, "Sonnet, effort=low"
    ),
}


def load(split: str | None) -> list[dict]:
    rows = [
        json.loads(line)
        for line in (HERE / "golden.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return [r for r in rows if split is None or r["split"] == split]


def to_email(email: dict) -> dict:
    return {
        "message_id": 0,
        "thread_id": None,
        "account_id": 0,
        "from_addr": email["from_addr"],
        "from_name": email.get("from_name"),
        "subject": email["subject"],
        "body_text": email["body"],
        "sent_at": "2026-09-12T09:00:00+05:00",
        "has_attachments": bool(email.get("attachments")),
        "is_test": False,
        "headers": email.get("headers", {}),
    }


def to_attachments(items: list[dict]) -> list[dict]:
    return [
        {
            "attachment_id": i,
            "filename": a.get("filename", "attachment"),
            "text": a["text"],
            "method": "text_layer",
        }
        for i, a in enumerate(items)
    ]


async def classify_all(classifier: ClaudeClassifier, rows: list[dict]) -> list[dict]:
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async def one(row: dict) -> dict:
        async with semaphore:
            triage = await classifier.classify(
                to_email(row["email"]),
                to_attachments(row.get("attachments", [])),
                [],
                row.get("sender"),
            )
        return {"id": row["id"], "expected": row["expected"], "tags": row["tags"], "got": triage}

    return await asyncio.gather(*(one(row) for row in rows))


def _amounts(items: list[dict]) -> set[tuple]:
    return {(float(a["value"]), a.get("currency", "")) for a in items}


def macro_f1(results: list[dict]) -> float:
    scores = []
    for level in IMPORTANCE_LEVELS:
        tp = sum(
            r["got"]["importance"] == level and r["expected"]["importance"] == level
            for r in results
        )
        fp = sum(
            r["got"]["importance"] == level and r["expected"]["importance"] != level
            for r in results
        )
        fn = sum(
            r["got"]["importance"] != level and r["expected"]["importance"] == level
            for r in results
        )
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return statistics.mean(scores)


def evaluate(results: list[dict]) -> dict[str, Any]:
    n = len(results)
    important = [r for r in results if r["expected"]["importance"] >= 2]
    suspicious = [r for r in results if r["expected"]["suspicious"]]
    flagged = [r for r in results if r["got"]["suspicious"]]
    with_entities = [r for r in results if r["expected"]["amounts"] or r["expected"]["deadlines"]]

    metrics = {
        "importance_accuracy": sum(
            r["got"]["importance"] == r["expected"]["importance"] for r in results
        )
        / n,
        "importance_macro_f1": macro_f1(results),
        "recall_ge2": (
            sum(r["got"]["importance"] >= 2 for r in important) / len(important)
            if important
            else 1.0
        ),
        "category_accuracy": sum(r["got"]["category"] == r["expected"]["category"] for r in results)
        / n,
        "needs_reply_accuracy": sum(
            r["got"]["needs_reply"] == r["expected"]["needs_reply"] for r in results
        )
        / n,
        "suspicious_recall": (
            sum(r["got"]["suspicious"] for r in suspicious) / len(suspicious) if suspicious else 1.0
        ),
        "suspicious_precision": (
            sum(r["expected"]["suspicious"] for r in flagged) / len(flagged) if flagged else 1.0
        ),
        "extraction_exact": (
            sum(
                _amounts(r["got"]["extracted"].get("amounts", []))
                == _amounts(r["expected"]["amounts"])
                and set(r["got"]["extracted"].get("deadlines", []))
                == set(r["expected"]["deadlines"])
                for r in with_entities
            )
            / len(with_entities)
            if with_entities
            else 1.0
        ),
    }
    costs = [r["got"]["meta"]["cost_usd"] for r in results]
    latencies = sorted(r["got"]["meta"]["latency_ms"] for r in results)
    metrics["avg_cost_usd"] = round(statistics.mean(costs), 6)
    metrics["total_cost_usd"] = round(sum(costs), 4)
    metrics["p95_latency_ms"] = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    return metrics


def confusions(results: list[dict]) -> list[str]:
    lines = []
    for r in results:
        exp, got = r["expected"], r["got"]
        problems = []
        if exp["importance"] != got["importance"]:
            problems.append(f"важность {exp['importance']}→{got['importance']}")
        if exp["suspicious"] != got["suspicious"]:
            problems.append(f"suspicious {exp['suspicious']}→{got['suspicious']}")
        if problems:
            lines.append(
                f"- `{r['id']}` ({', '.join(r['tags'])}): {'; '.join(problems)} — «{got['reasoning'][:80]}»"
            )
    return lines


def render(report: dict, split: str | None) -> str:
    lines = [
        "# Оценка классификации",
        "",
        f"Датасет: {report['n']} писем ({split or 'все'} split). "
        "Сгенерировано `evals/classification/run.py`.",
        "",
        "## Метрики по конфигурациям",
        "",
        "| Конфигурация | accuracy | macro-F1 | recall ≥2 | категория "
        "| suspicious recall | извлечение | $/письмо | p95 latency |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for cfg_name, data in report["configs"].items():
        m = data["metrics"]
        label = CONFIGS[cfg_name].label
        lines.append(
            f"| {label} | {m['importance_accuracy']:.2f} | {m['importance_macro_f1']:.2f} "
            f"| {m['recall_ge2']:.2f} | {m['category_accuracy']:.2f} | {m['suspicious_recall']:.2f} "
            f"| {m['extraction_exact']:.2f} | ${m['avg_cost_usd']:.4f} | {m['p95_latency_ms']} мс |"
        )
    prod = report["configs"].get("prod")
    if prod:
        lines += ["", "## Ошибки prod-конфигурации (Haiku, t=0)", ""]
        errors = confusions(prod["results"])
        lines += errors if errors else ["Нет: все важности и suspicious совпали."]
    return "\n".join(lines) + "\n"


async def main(split: str | None, config_names: list[str]) -> None:
    rows = load(split)
    skills = SkillRegistry.load()
    client = make_client()
    report: dict = {"n": len(rows), "configs": {}}
    try:
        for name in config_names:
            cfg = CONFIGS[name]
            classifier = ClaudeClassifier(
                client,
                skills,
                today=lambda: TODAY,
                model=cfg.model,
                prompt_version=f"classify-eval-{name}",
                sampling=cfg.sampling,
            )
            results = await classify_all(classifier, rows)
            report["configs"][name] = {"metrics": evaluate(results), "results": results}
            print(
                f"[{cfg.label}] "
                + ", ".join(f"{k}={v}" for k, v in report["configs"][name]["metrics"].items())
            )
    finally:
        await client.close()

    serializable = {
        "n": report["n"],
        "configs": {k: {"metrics": v["metrics"]} for k, v in report["configs"].items()},
    }
    (HERE / "results.json").write_text(json.dumps(serializable, ensure_ascii=False, indent=2))
    markdown = render(report, split)
    (HERE / "results.md").write_text(markdown, encoding="utf-8")
    print("\n" + markdown)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--split", choices=["dev", "test"], default=None)
    parser.add_argument("--configs", default="prod,sonnet,haiku-t05,haiku-t10")
    args = parser.parse_args()
    asyncio.run(main(args.split, args.configs.split(",")))
