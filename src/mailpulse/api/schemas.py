"""Схемы запросов и ответов Mini App."""

from pydantic import BaseModel, Field


class AccountIn(BaseModel):
    email: str
    password: str = Field(min_length=1, description="Пароль приложения (Gmail, Яндекс)")
    provider: str | None = Field(default=None, description="gmail — для Gmail на своём домене")


class AccountOut(BaseModel):
    id: int
    email: str
    provider: str
    status: str
    last_error: str | None


class RuleIn(BaseModel):
    kind: str = Field(description="vip | mute")
    value: str = Field(min_length=1, description="адрес или @домен")


class RuleOut(BaseModel):
    id: int
    kind: str
    value: str


class ImportanceBucket(BaseModel):
    importance: int
    count: int


class RecentEmail(BaseModel):
    message_id: int
    from_addr: str
    subject: str
    importance: int
    category: str
    needs_reply: bool


class Stats(BaseModel):
    accounts: int
    messages: int
    processed: int
    by_importance: list[ImportanceBucket]
    total_cost_usd: float
    recent: list[RecentEmail]


class Me(BaseModel):
    user_id: int
    first_name: str
    username: str | None
