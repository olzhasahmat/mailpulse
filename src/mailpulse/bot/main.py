"""Telegram-бот: регистрация пользователя и кнопки на карточках писем.

Бот не вызывает LLM: нажатие превращается в задачу resume_email, граф продолжает воркер.
«Изменить» переводит в режим правки: следующий текст становится указанием для черновика.
"""

import asyncio
import logging

from aiogram import Dispatcher, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from mailpulse.agent.adapters.postgres import owner_of_message
from mailpulse.bot.cards import ACTION_RESULTS, CALLBACK_PREFIX, parse_callback_data
from mailpulse.bot.notifier import create_bot
from mailpulse.config import get_settings
from mailpulse.db.models import User
from mailpulse.db.session import make_engine, make_sessionmaker
from mailpulse.observability import setup_logging
from mailpulse.worker import queue

log = logging.getLogger(__name__)
router = Router()

# tg_user_id -> message_id, по которому ждём текст правки черновика. В памяти: одна реплика бота
AwaitingEdit = dict[int, int]


async def _enqueue_resume(sessionmaker, message_id: int, answer: dict) -> None:
    async with sessionmaker.begin() as session:
        await queue.enqueue(session, "resume_email", {"message_id": message_id, "answer": answer})


WELCOME = (
    "👋 <b>MailPulse</b> — ассистент для входящей почты.\n\n"
    "Я читаю ваши ящики (Gmail, Яндекс) и присылаю сюда <b>только важные</b> письма — "
    "с сутью, суммой и сроком, и сразу с кнопкой «Ответить». "
    "Реклама, счета и шум остаются в ящике.\n\n"
    "Чтобы начать, подключите почту 👇"
)
CONNECTED_HINT = (
    "\n\nПодключить ящик можно и из терминала: <code>mailpulse-accounts add you@gmail.com</code>"
)


def _connect_keyboard(miniapp_url: str | None) -> InlineKeyboardMarkup | None:
    if not miniapp_url or not miniapp_url.startswith("https://"):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📮 Подключить почту", web_app=WebAppInfo(url=miniapp_url))]
        ]
    )


@router.message(CommandStart())
async def start(
    message: Message, command: CommandObject, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    if message.from_user is None:
        return
    # t.me/bot?start=<referral> — метка рекламного источника, ставится один раз
    referral = (command.args or "").strip()[:64] or None
    stmt = (
        insert(User)
        .values(tg_user_id=message.from_user.id, tg_chat_id=message.chat.id, referral=referral)
        .on_conflict_do_update(
            index_elements=[User.tg_user_id],
            set_={
                "tg_chat_id": message.chat.id,
                # первый источник не перезаписываем при повторном /start
                "referral": func.coalesce(User.referral, insert(User).excluded.referral),
            },
        )
    )
    async with sessionmaker.begin() as session:
        await session.execute(stmt)

    settings = get_settings()
    keyboard = _connect_keyboard(settings.miniapp_url)
    text = WELCOME if keyboard else WELCOME + CONNECTED_HINT
    await message.answer(text, reply_markup=keyboard)


@router.callback_query(F.data.startswith(f"{CALLBACK_PREFIX}:"))
async def on_card_action(
    callback: CallbackQuery,
    sessionmaker: async_sessionmaker[AsyncSession],
    awaiting_edit: AwaitingEdit,
) -> None:
    parsed = parse_callback_data(callback.data or "")
    if parsed is None:
        await callback.answer("Кнопка устарела")
        return
    message_id, action = parsed

    async with sessionmaker() as session:
        owner = await owner_of_message(session, message_id)
    if owner is None or owner.tg_user_id != callback.from_user.id:
        await callback.answer("Это письмо не из ваших ящиков", show_alert=True)
        return

    if action == "edit":
        # Не возобновляем граф сразу: ждём от пользователя текст правки
        awaiting_edit[callback.from_user.id] = message_id
        await callback.answer()
        await callback.message.answer("Напишите, что поправить в ответе.")
        return

    await _enqueue_resume(sessionmaker, message_id, {"action": action})
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"{callback.message.html_text}\n\n<i>{ACTION_RESULTS[action]}</i>", reply_markup=None
        )


@router.message(F.text & ~F.text.startswith("/"))
async def on_edit_instruction(
    message: Message,
    sessionmaker: async_sessionmaker[AsyncSession],
    awaiting_edit: AwaitingEdit,
) -> None:
    if message.from_user is None:
        return
    message_id = awaiting_edit.pop(message.from_user.id, None)
    if message_id is None:
        return  # обычное сообщение вне режима правки — игнорируем
    await _enqueue_resume(sessionmaker, message_id, {"action": "edit", "text": message.text})
    await message.answer("✏️ Правлю черновик…")


def create_dispatcher(sessionmaker: async_sessionmaker[AsyncSession]) -> Dispatcher:
    # Именованные аргументы Dispatcher попадают в хендлеры по имени параметра
    dispatcher = Dispatcher(sessionmaker=sessionmaker, awaiting_edit={})
    dispatcher.include_router(router)
    return dispatcher


async def main() -> None:
    settings = get_settings()
    if settings.telegram_bot_token is None:
        raise SystemExit("TELEGRAM_BOT_TOKEN не задан")
    engine = make_engine()
    bot = create_bot(settings.telegram_bot_token.get_secret_value())
    try:
        await create_dispatcher(make_sessionmaker(engine)).start_polling(bot)
    finally:
        await bot.session.close()
        await engine.dispose()


def run() -> None:
    setup_logging(get_settings().log_level)
    asyncio.run(main())
