"""Модели БД. Источник правды для alembic autogenerate."""

import enum
from datetime import datetime, time
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Computed,
    DateTime,
    Enum,
    ForeignKey,
    Identity,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from mailpulse.db.base import Base

# bge-m3 и Voyage дают 1024 измерения; модель с другой размерностью = миграция и переиндексация
EMBEDDING_DIM = 1024


class Provider(enum.StrEnum):
    GMAIL = "gmail"
    MICROSOFT = "microsoft"
    IMAP = "imap"


class AuthType(enum.StrEnum):
    APP_PASSWORD = "app_password"
    XOAUTH2 = "xoauth2"


class AccountStatus(enum.StrEnum):
    ACTIVE = "active"
    AUTH_FAILED = "auth_failed"
    DISABLED = "disabled"


class ExtractionMethod(enum.StrEnum):
    TEXT_LAYER = "text_layer"
    DOCX = "docx"
    HTML = "html"
    VISION = "vision"
    NONE = "none"


class RuleKind(enum.StrEnum):
    VIP = "vip"
    MUTE = "mute"
    INSTRUCTION = "instruction"


class NotificationKind(enum.StrEnum):
    CARD = "card"
    DIGEST = "digest"
    QUARANTINE = "quarantine"


class DraftStatus(enum.StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    FAILED = "failed"


class FeedbackKind(enum.StrEnum):
    IMPORTANCE_WRONG = "importance_wrong"
    IMPORTANCE_RIGHT = "importance_right"
    DRAFT_EDITED = "draft_edited"
    DRAFT_REJECTED = "draft_rejected"


class JobStatus(enum.StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


def _enum(cls: type[enum.Enum]) -> Enum:
    # VARCHAR вместо нативного enum Postgres: новое значение не требует ALTER TYPE
    return Enum(cls, native_enum=False, length=32, values_callable=lambda e: [m.value for m in e])


def _pk() -> Mapped[int]:
    return mapped_column(BigInteger, Identity(), primary_key=True)


def _fk(target: str, *, nullable: bool = False, ondelete: str = "CASCADE") -> Mapped[Any]:
    return mapped_column(
        BigInteger, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=True
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = _pk()
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    tg_chat_id: Mapped[int] = mapped_column(BigInteger)
    timezone: Mapped[str] = mapped_column(String(64), server_default="Asia/Almaty")
    digest_time: Mapped[time] = mapped_column(Time, server_default=text("'09:00'"))
    daily_budget_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), server_default="1.00")
    created_at: Mapped[datetime] = _created_at()


class MailAccount(Base):
    __tablename__ = "mail_accounts"
    __table_args__ = (UniqueConstraint("user_id", "email"),)

    id: Mapped[int] = _pk()
    user_id: Mapped[int] = _fk("users.id")
    provider: Mapped[Provider] = mapped_column(_enum(Provider))
    email: Mapped[str] = mapped_column(String(320))
    auth_type: Mapped[AuthType] = mapped_column(_enum(AuthType))
    # Пароль приложения или refresh-токен, зашифрованный SecretsCipher
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    imap_host: Mapped[str] = mapped_column(String(255))
    imap_port: Mapped[int] = mapped_column(Integer, server_default="993")
    smtp_host: Mapped[str] = mapped_column(String(255))
    smtp_port: Mapped[int] = mapped_column(Integer, server_default="587")
    status: Mapped[AccountStatus] = mapped_column(
        _enum(AccountStatus), server_default=AccountStatus.ACTIVE.value
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class MailboxSync(Base):
    """Позиция синхронизации папки. Смена UIDVALIDITY означает, что старые UID недействительны."""

    __tablename__ = "mailbox_sync"

    account_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("mail_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(String(255), primary_key=True)
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, server_default="0")
    updated_at: Mapped[datetime] = _updated_at()


class Thread(Base):
    __tablename__ = "threads"
    __table_args__ = (Index("ix_threads_user_subject", "user_id", "subject_norm"),)

    id: Mapped[int] = _pk()
    user_id: Mapped[int] = _fk("users.id")
    subject_norm: Mapped[str] = mapped_column(Text)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("account_id", "message_id_hdr"),
        Index("ix_messages_account_folder_uid", "account_id", "folder", "uid"),
        # Поиск родителя по In-Reply-To / References во всех ящиках пользователя
        Index("ix_messages_message_id_hdr", "message_id_hdr"),
    )

    id: Mapped[int] = _pk()
    account_id: Mapped[int] = _fk("mail_accounts.id")
    thread_id: Mapped[int | None] = _fk("threads.id", nullable=True, ondelete="SET NULL")
    folder: Mapped[str] = mapped_column(String(255))
    uid: Mapped[int] = mapped_column(BigInteger)
    message_id_hdr: Mapped[str] = mapped_column(String(998))
    in_reply_to: Mapped[str | None] = mapped_column(String(998))
    references: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'"))
    from_addr: Mapped[str] = mapped_column(String(320), index=True)
    from_name: Mapped[str | None] = mapped_column(String(255))
    to_addrs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    cc_addrs: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    subject: Mapped[str] = mapped_column(Text, server_default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Текст без HTML и без цитат предыдущих писем
    body_text: Mapped[str] = mapped_column(Text, server_default="")
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    has_attachments: Mapped[bool] = mapped_column(server_default=text("false"))
    # Отправлено самим пользователем: хранится для тредов и истории, но не разбирается
    outgoing: Mapped[bool] = mapped_column(server_default=text("false"))
    # Письмо подложено make inject-email: разбирается и шлёт карточку, но не идёт
    # в RAG-индекс, историю отправителей и статистику
    is_test: Mapped[bool] = mapped_column(server_default=text("false"))
    created_at: Mapped[datetime] = _created_at()


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = _pk()
    message_id: Mapped[int] = _fk("messages.id")
    filename: Mapped[str] = mapped_column(String(512))
    mime: Mapped[str] = mapped_column(String(255))
    size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text)
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[ExtractionMethod | None] = mapped_column(_enum(ExtractionMethod))
    created_at: Mapped[datetime] = _created_at()


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
    )

    id: Mapped[int] = _pk()
    # Каждый поиск обязан фильтровать по user_id: иначе RAG найдёт чужие письма
    user_id: Mapped[int] = _fk("users.id")
    message_id: Mapped[int] = _fk("messages.id")
    attachment_id: Mapped[int | None] = _fk("attachments.id", nullable=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    # 'simple' без стемминга: письма смешивают русский, казахский и английский
    tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', content)", persisted=True)
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()


class SenderProfile(Base):
    __tablename__ = "sender_profiles"

    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    address: Mapped[str] = mapped_column(String(320), primary_key=True)
    msg_count: Mapped[int] = mapped_column(Integer, server_default="0")
    reply_count: Mapped[int] = mapped_column(Integer, server_default="0")
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _updated_at()


class UserRule(Base):
    __tablename__ = "user_rules"

    id: Mapped[int] = _pk()
    user_id: Mapped[int] = _fk("users.id")
    kind: Mapped[RuleKind] = mapped_column(_enum(RuleKind))
    value: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class TriageResult(Base):
    __tablename__ = "triage_results"
    __table_args__ = (CheckConstraint("importance BETWEEN 0 AND 3", name="importance_range"),)

    message_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    importance: Mapped[int] = mapped_column(SmallInteger)
    category: Mapped[str] = mapped_column(String(32))
    needs_reply: Mapped[bool]
    suspicious: Mapped[bool] = mapped_column(server_default=text("false"))
    reasoning: Mapped[str] = mapped_column(Text)
    extracted: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    # Без модели и версии промпта не сравнить варианты A/B на реальном потоке
    model: Mapped[str] = mapped_column(String(64))
    prompt_version: Mapped[str] = mapped_column(String(32))
    latency_ms: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime] = _created_at()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[int] = _pk()
    message_id: Mapped[int] = _fk("messages.id")
    kind: Mapped[NotificationKind] = mapped_column(_enum(NotificationKind))
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime] = _created_at()


class Draft(Base):
    __tablename__ = "drafts"
    __table_args__ = (UniqueConstraint("message_id", "version"),)

    id: Mapped[int] = _pk()
    message_id: Mapped[int] = _fk("messages.id")
    version: Mapped[int] = mapped_column(Integer)
    body: Mapped[str] = mapped_column(Text)
    # Отправка разрешена только из approved; approved ставит только нажатие в Telegram
    status: Mapped[DraftStatus] = mapped_column(
        _enum(DraftStatus), server_default=DraftStatus.PENDING.value
    )
    feedback: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Feedback(Base):
    __tablename__ = "feedback"

    id: Mapped[int] = _pk()
    user_id: Mapped[int] = _fk("users.id")
    message_id: Mapped[int] = _fk("messages.id")
    kind: Mapped[FeedbackKind] = mapped_column(_enum(FeedbackKind))
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = _created_at()


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_user_created", "user_id", "created_at"),)

    id: Mapped[int] = _pk()
    user_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="SET NULL")
    )
    node: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cache_write_tokens: Mapped[int] = mapped_column(Integer, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6))
    created_at: Mapped[datetime] = _created_at()


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index(
            "ix_jobs_queued",
            "run_after",
            postgresql_where=text("status = 'queued'"),
        ),
    )

    id: Mapped[int] = _pk()
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus), server_default=JobStatus.QUEUED.value
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, server_default="5")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
