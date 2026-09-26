"""Зависимости FastAPI: сессии БД и текущий пользователь из Telegram initData."""

import logging

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.api.auth import InitDataError, verify_init_data
from mailpulse.config import Settings, get_settings
from mailpulse.db.models import User

log = logging.getLogger(__name__)


def get_sessionmaker(request: Request) -> async_sessionmaker[AsyncSession]:
    return request.app.state.sessionmaker


def get_bot(request: Request):
    """Бот для отправки уведомлений из API. None, если TELEGRAM_BOT_TOKEN не задан."""
    return getattr(request.app.state, "bot", None)


async def current_user(
    request: Request,
    authorization: str = Header(default=""),
    settings: Settings = Depends(get_settings),
    sessionmaker: async_sessionmaker[AsyncSession] = Depends(get_sessionmaker),
) -> User:
    """Пользователь из заголовка `Authorization: tma <initData>`; создаётся при первом заходе."""
    if settings.telegram_bot_token is None:
        raise HTTPException(503, "TELEGRAM_BOT_TOKEN не задан")
    scheme, _, init_data = authorization.partition(" ")
    if scheme.lower() != "tma" or not init_data:
        raise HTTPException(401, "нужен заголовок Authorization: tma <initData>")
    try:
        tg = verify_init_data(init_data, settings.telegram_bot_token.get_secret_value())
    except InitDataError as exc:
        raise HTTPException(401, f"initData не прошёл проверку: {exc}") from exc

    stmt = (
        insert(User)
        .values(tg_user_id=tg.tg_user_id, tg_chat_id=tg.tg_user_id)
        .on_conflict_do_update(index_elements=[User.tg_user_id], set_={"tg_user_id": tg.tg_user_id})
        .returning(User.id)
    )
    async with sessionmaker.begin() as session:
        user_id = (await session.execute(stmt)).scalar_one()
        return await session.get(User, user_id)
