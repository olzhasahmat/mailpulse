"""Notifier через Telegram: карточки писем, карантин, черновики."""

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.state import EmailView, GuardrailVerdict, Summary, TriageResult
from mailpulse.bot.cards import (
    callback_data,
    card_buttons,
    render_card,
    render_draft,
    render_quarantine,
)
from mailpulse.db.models import Notification, NotificationKind, User


def create_bot(token: str) -> Bot:
    return Bot(
        token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )


REVIEW_BUTTONS = (("send", "📤 Отправить"), ("edit", "✏️ Изменить"), ("cancel", "Отмена"))


def _keyboard(message_id: int, buttons: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(text=label, callback_data=callback_data(message_id, action))
        for action, label in buttons
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


class TelegramNotifier:
    def __init__(self, bot: Bot, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._bot = bot
        self._sessionmaker = sessionmaker

    async def send_card(
        self,
        user_id: int,
        message_id: int,
        email: EmailView,
        triage: TriageResult,
        summary: Summary,
    ) -> None:
        # Узел может выполниться повторно после сбоя — вторая карточка пользователю не нужна
        if await self._already_sent(message_id, NotificationKind.CARD):
            return
        sent = await self._bot.send_message(
            await self._chat_id(user_id),
            render_card(email, triage, summary),
            reply_markup=_keyboard(message_id, card_buttons(triage["needs_reply"])),
        )
        await self._record(message_id, NotificationKind.CARD, sent.message_id)

    async def send_quarantine(
        self, user_id: int, message_id: int, email: EmailView, verdict: GuardrailVerdict
    ) -> None:
        if await self._already_sent(message_id, NotificationKind.QUARANTINE):
            return
        sent = await self._bot.send_message(
            await self._chat_id(user_id), render_quarantine(email, verdict)
        )
        await self._record(message_id, NotificationKind.QUARANTINE, sent.message_id)

    async def send_draft(self, user_id: int, message_id: int, draft_id: int, body: str) -> None:
        await self._bot.send_message(
            await self._chat_id(user_id),
            render_draft(body),
            reply_markup=_keyboard(message_id, REVIEW_BUTTONS),
        )

    async def _chat_id(self, user_id: int) -> int:
        async with self._sessionmaker() as session:
            chat_id = await session.scalar(select(User.tg_chat_id).where(User.id == user_id))
        if chat_id is None:
            raise LookupError(f"пользователь {user_id} не найден")
        return chat_id

    async def _already_sent(self, message_id: int, kind: NotificationKind) -> bool:
        stmt = select(Notification.id).where(
            Notification.message_id == message_id, Notification.kind == kind
        )
        async with self._sessionmaker() as session:
            return await session.scalar(stmt.limit(1)) is not None

    async def _record(self, message_id: int, kind: NotificationKind, tg_message_id: int) -> None:
        stmt = insert(Notification).values(
            message_id=message_id, kind=kind, tg_message_id=tg_message_id
        )
        async with self._sessionmaker.begin() as session:
            await session.execute(stmt)
