"""Эндпоинты Mini App: ящики, правила, статистика. Все скоупятся по текущему пользователю."""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from imapclient import IMAPClient
from imapclient.exceptions import LoginError
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.api.deps import current_user, get_sessionmaker
from mailpulse.api.schemas import (
    AccountIn,
    AccountOut,
    ImportanceBucket,
    Me,
    RecentEmail,
    RuleIn,
    RuleOut,
    Stats,
)
from mailpulse.config import Settings, get_settings
from mailpulse.db import models as db
from mailpulse.mail import accounts as mail_accounts
from mailpulse.security import SecretsCipher

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

RECENT_LIMIT = 20
ALLOWED_RULE_KINDS = {"vip": db.RuleKind.VIP, "mute": db.RuleKind.MUTE}


@router.get("/me")
async def me(user: db.User = Depends(current_user)) -> Me:
    return Me(user_id=user.id, first_name="", username=None)


@router.get("/accounts")
async def list_accounts(
    user: db.User = Depends(current_user),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> list[AccountOut]:
    stmt = (
        select(db.MailAccount).where(db.MailAccount.user_id == user.id).order_by(db.MailAccount.id)
    )
    async with sessionmaker() as session:
        return [_account_out(a) for a in (await session.execute(stmt)).scalars()]


@router.post("/accounts", status_code=201)
async def connect_account(
    body: AccountIn,
    user: db.User = Depends(current_user),
    settings: Settings = Depends(get_settings),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> AccountOut:
    if settings.secrets_key is None:
        raise HTTPException(503, "SECRETS_KEY не настроен на сервере")
    provider = db.Provider(body.provider) if body.provider else None
    preset = mail_accounts.preset_for(body.email, provider)
    if preset is None:
        raise HTTPException(
            400, "Неизвестный почтовый сервис. Для Gmail на своём домене — provider=gmail"
        )
    if preset.provider is db.Provider.MICROSOFT:
        raise HTTPException(400, "Microsoft 365 подключается через OAuth — появится позже")

    password = (
        body.password.replace(" ", "") if preset.provider is db.Provider.GMAIL else body.password
    )
    if not await asyncio.to_thread(_imap_login_ok, preset, body.email, password):
        raise HTTPException(400, "Не удалось войти: проверьте адрес и пароль приложения")

    cipher = SecretsCipher.from_settings(settings)
    async with sessionmaker.begin() as session:
        account_id = await mail_accounts.upsert_account(
            session,
            user_id=user.id,
            email=body.email,
            preset=preset,
            auth_type=db.AuthType.APP_PASSWORD,
            secret_enc=cipher.encrypt(password),
        )
        account = await session.get(db.MailAccount, account_id)
        return _account_out(account)


@router.delete("/accounts/{account_id}", status_code=204)
async def disconnect_account(
    account_id: int,
    user: db.User = Depends(current_user),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> None:
    async with sessionmaker.begin() as session:
        account = await session.get(db.MailAccount, account_id)
        if account is None or account.user_id != user.id:
            raise HTTPException(404, "Ящик не найден")
        await mail_accounts.set_status(session, account_id, db.AccountStatus.DISABLED)


@router.get("/rules")
async def list_rules(
    user: db.User = Depends(current_user),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> list[RuleOut]:
    stmt = (
        select(db.UserRule)
        .where(db.UserRule.user_id == user.id, db.UserRule.kind.in_(ALLOWED_RULE_KINDS.values()))
        .order_by(db.UserRule.id)
    )
    async with sessionmaker() as session:
        rules = (await session.execute(stmt)).scalars()
        return [RuleOut(id=r.id, kind=r.kind.value, value=r.value) for r in rules]


@router.post("/rules", status_code=201)
async def add_rule(
    body: RuleIn,
    user: db.User = Depends(current_user),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> RuleOut:
    kind = ALLOWED_RULE_KINDS.get(body.kind)
    if kind is None:
        raise HTTPException(400, "kind должен быть vip или mute")
    async with sessionmaker.begin() as session:
        rule = db.UserRule(user_id=user.id, kind=kind, value=body.value.strip().casefold())
        session.add(rule)
        await session.flush()
        return RuleOut(id=rule.id, kind=rule.kind.value, value=rule.value)


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: int,
    user: db.User = Depends(current_user),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> None:
    async with sessionmaker.begin() as session:
        result = await session.execute(
            delete(db.UserRule).where(db.UserRule.id == rule_id, db.UserRule.user_id == user.id)
        )
        if result.rowcount == 0:
            raise HTTPException(404, "Правило не найдено")


@router.get("/stats")
async def stats(
    user: db.User = Depends(current_user),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> Stats:
    own_messages = (
        select(db.Message.id)
        .join(db.MailAccount, db.MailAccount.id == db.Message.account_id)
        .where(db.MailAccount.user_id == user.id, db.Message.is_test.is_(False))
    )
    async with sessionmaker() as session:
        accounts_count = await session.scalar(
            select(func.count())
            .select_from(db.MailAccount)
            .where(
                db.MailAccount.user_id == user.id,
                db.MailAccount.status != db.AccountStatus.DISABLED,
            )
        )
        messages_count = await session.scalar(
            select(func.count()).select_from(own_messages.subquery())
        )
        buckets = (
            await session.execute(
                select(db.TriageResult.importance, func.count())
                .where(db.TriageResult.message_id.in_(own_messages))
                .group_by(db.TriageResult.importance)
                .order_by(db.TriageResult.importance.desc())
            )
        ).all()
        cost = await session.scalar(
            select(func.coalesce(func.sum(db.LlmUsage.cost_usd), 0)).where(
                db.LlmUsage.user_id == user.id
            )
        )
        recent = await _recent(session, own_messages)

    processed = sum(count for _, count in buckets)
    return Stats(
        accounts=accounts_count,
        messages=messages_count,
        processed=processed,
        by_importance=[ImportanceBucket(importance=i, count=c) for i, c in buckets],
        total_cost_usd=round(float(cost), 4),
        recent=recent,
    )


async def _recent(session: AsyncSession, own_messages) -> list[RecentEmail]:
    triage = db.TriageResult
    stmt = (
        select(db.Message, triage)
        .join(triage, triage.message_id == db.Message.id)
        .where(db.Message.id.in_(own_messages), triage.importance >= 2)
        .order_by(db.Message.sent_at.desc().nulls_last(), db.Message.id.desc())
        .limit(RECENT_LIMIT)
    )
    rows = (await session.execute(stmt)).all()
    return [
        RecentEmail(
            message_id=message.id,
            from_addr=message.from_addr,
            subject=message.subject,
            importance=t.importance,
            category=t.category,
            needs_reply=t.needs_reply,
        )
        for message, t in rows
    ]


def _account_out(account: db.MailAccount) -> AccountOut:
    return AccountOut(
        id=account.id,
        email=account.email,
        provider=account.provider.value,
        status=account.status.value,
        last_error=account.last_error,
    )


def _imap_login_ok(preset, email: str, password: str) -> bool:
    try:
        with IMAPClient(preset.imap_host, port=preset.imap_port, ssl=True, timeout=30) as client:
            client.login(email, password)
        return True
    except LoginError:
        return False
    except Exception as exc:
        log.warning("IMAP проверка %s не удалась: %s", email, exc)
        return False
