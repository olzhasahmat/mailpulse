"""Извлечение текста из вложений: текстовый слой PDF, DOCX, HTML и vision для сканов и картинок.

Модуль без сети и без LLM: vision вынесен в отдельную функцию-порт, чтобы разбор форматов
тестировался офлайн.
"""

import io
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from mailpulse.mail.parsing import html_to_text

log = logging.getLogger(__name__)

MAX_TEXT_CHARS = 20_000
# Ниже этого порога считаем, что у PDF нет текстового слоя (скан) — идём в vision
PDF_TEXT_MIN_CHARS = 20
IMAGE_MIME_PREFIX = "image/"
VISION_MIME = {"application/pdf", "image/jpeg", "image/png", "image/gif", "image/webp"}

# (raw, mime) -> распознанный текст. Реализация на Claude в documents/vision.py
VisionExtractor = Callable[[bytes, str, str], Awaitable[str]]


@dataclass(frozen=True)
class Extracted:
    text: str
    method: str  # text_layer | docx | html | vision | none


def _clip(text: str) -> str:
    text = text.strip()
    return text[:MAX_TEXT_CHARS]


def extract_pdf_text(raw: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(raw))
        return _clip("\n".join(page.extract_text() or "" for page in reader.pages))
    except (PyPdfError, ValueError, OSError) as exc:
        log.warning("pdf text extraction failed: %s", exc)
        return ""


def extract_docx_text(raw: bytes) -> str:
    from docx import Document
    from docx.opc.exceptions import PackageNotFoundError

    try:
        document = Document(io.BytesIO(raw))
    except (PackageNotFoundError, ValueError, KeyError) as exc:
        log.warning("docx extraction failed: %s", exc)
        return ""
    parts = [p.text for p in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            parts.append(" | ".join(cell.text for cell in row.cells))
    return _clip("\n".join(parts))


def extract_html_text(raw: bytes) -> str:
    text, _hidden = html_to_text(raw.decode("utf-8", errors="replace"))
    return _clip(text)


async def extract_attachment(
    raw: bytes, mime: str, filename: str, vision: VisionExtractor | None
) -> Extracted:
    """Выбирает способ по типу вложения. vision=None — сканы и картинки не распознаём."""
    mime = mime.lower()
    name = filename.lower()

    if mime == "application/pdf" or name.endswith(".pdf"):
        text = extract_pdf_text(raw)
        if len(text) >= PDF_TEXT_MIN_CHARS:
            return Extracted(text, "text_layer")
        # PDF без текстового слоя — скан; отдаём файл целиком в vision
        return await _vision(raw, "application/pdf", filename, vision)

    if name.endswith((".docx",)) or "wordprocessingml" in mime:
        return Extracted(extract_docx_text(raw), "docx")

    if mime in ("text/html", "application/xhtml+xml") or name.endswith((".html", ".htm")):
        return Extracted(extract_html_text(raw), "html")

    if mime.startswith(IMAGE_MIME_PREFIX):
        return await _vision(raw, mime, filename, vision)

    if mime.startswith("text/") or name.endswith(".txt"):
        return Extracted(_clip(raw.decode("utf-8", errors="replace")), "text_layer")

    return Extracted("", "none")


async def _vision(
    raw: bytes, mime: str, filename: str, vision: VisionExtractor | None
) -> Extracted:
    if vision is None or mime not in VISION_MIME:
        return Extracted("", "none")
    try:
        return Extracted(_clip(await vision(raw, mime, filename)), "vision")
    except Exception as exc:
        log.warning("vision extraction failed for %s: %s", filename, exc)
        return Extracted("", "none")
