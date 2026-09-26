from dataclasses import dataclass, field

import pytest

from mailpulse.bot.main import _connect_keyboard, start
from mailpulse.db import models

pytestmark = pytest.mark.db


def test_connect_keyboard_needs_https_url():
    assert _connect_keyboard(None) is None
    assert _connect_keyboard("http://insecure.example") is None
    kb = _connect_keyboard("https://app.example")
    assert kb.inline_keyboard[0][0].web_app.url == "https://app.example"


@dataclass
class FakeUser:
    id: int


@dataclass
class FakeChat:
    id: int


@dataclass
class FakeMessage:
    from_user: FakeUser
    chat: FakeChat
    replies: list = field(default_factory=list)

    async def answer(self, text, reply_markup=None):
        self.replies.append((text, reply_markup))


@dataclass
class FakeCommand:
    args: str | None


async def _user(db, tg_user_id: int):
    from sqlalchemy import select

    async with db() as session:
        return (
            await session.execute(select(models.User).where(models.User.tg_user_id == tg_user_id))
        ).scalar_one()


async def test_start_captures_referral_and_replies(db):
    msg = FakeMessage(FakeUser(101), FakeChat(101))

    await start(msg, FakeCommand("insta_ad1"), db)

    user = await _user(db, 101)
    assert user.referral == "insta_ad1"
    assert "MailPulse" in msg.replies[0][0]


async def test_first_referral_is_kept_on_repeat_start(db):
    await start(FakeMessage(FakeUser(102), FakeChat(102)), FakeCommand("first_source"), db)
    await start(FakeMessage(FakeUser(102), FakeChat(102)), FakeCommand("second_source"), db)

    user = await _user(db, 102)
    assert user.referral == "first_source"  # первый источник не перезаписан


async def test_start_without_referral(db):
    await start(FakeMessage(FakeUser(103), FakeChat(103)), FakeCommand(None), db)

    user = await _user(db, 103)
    assert user.referral is None
