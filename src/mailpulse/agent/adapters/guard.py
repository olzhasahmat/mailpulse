"""Эвристический guardrail: ловит явные попытки prompt injection до вызова LLM.

Проверку через LLM добавим на день 6; эвристика останется первым, бесплатным фильтром.
"""

import re

from mailpulse.agent.state import AttachmentText, EmailView, GuardrailVerdict
from mailpulse.mail.parsing import HIDDEN_TEXT_HEADER

INJECTION_PATTERNS = [
    re.compile(pattern, re.I)
    for pattern in (
        r"ignore\s+(all\s+|any\s+)?(previous|prior|above)\s+instructions",
        r"disregard\s+(all\s+|the\s+)?(previous|prior|above)",
        r"\b(ai|llm|gpt|claude)\s+assistant\s*:",
        r"\bsystem\s+prompt\b",
        r"игнорируй\s+(все\s+)?(предыдущие|прошлые|свои)\s+инструкции",
        r"забудь\s+(все\s+)?(предыдущие|прошлые|свои)\s+инструкции",
        r"(ии|ai)[-\s]ассистент\s*:",
    )
]


class HeuristicGuard:
    async def check(self, email: EmailView, attachments: list[AttachmentText]) -> GuardrailVerdict:
        sources = {
            "тексте письма": f"{email['subject']}\n{email['body_text']}",
            "скрытом тексте": email["headers"].get(HIDDEN_TEXT_HEADER, ""),
            "вложениях": "\n".join(a["text"] for a in attachments),
        }
        reasons = []
        for place, text in sources.items():
            for pattern in INJECTION_PATTERNS:
                if match := pattern.search(text):
                    reasons.append(f"инструкция для ИИ в {place}: «{match.group(0)}»")
                    break
        return {"suspicious": bool(reasons), "reasons": reasons}
