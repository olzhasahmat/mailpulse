from datetime import date
from email.message import EmailMessage

import pytest
from sqlalchemy import func, select

from mailpulse.db import models
from mailpulse.mail.archive import existing_message_ids, import_folder
from mailpulse.mail.sync import AccountRef

pytestmark = pytest.mark.db

ACCOUNT_EMAIL = "you@gmail.com"
SINCE = date(2026, 6, 1)


class FakeArchiveMailbox:
    """IMAP-заглушка: письма с фиксированными UID, ENVELOPE и телом."""

    def __init__(self, messages: dict[int, bytes]) -> None:
        self.messages = messages
        self.fetched_bodies: list[int] = []

    def select(self, folder: str):
        return None

    def uids_since(self, since: date) -> list[int]:
        return sorted(self.messages)

    def message_ids(self, uids: list[int]) -> dict[int, str | None]:
        result = {}
        for uid in uids:
            msg = self.messages[uid]
            line = next(
                (ln for ln in msg.decode().splitlines() if ln.startswith("Message-ID:")), None
            )
            result[uid] = line.split(": ", 1)[1] if line else None
        return result

    def fetch_raw(self, uids: list[int]) -> dict[int, bytes]:
        self.fetched_bodies.extend(uids)
        return {uid: self.messages[uid] for uid in uids}


def make_raw(
    message_id: str, *, sender="boss@company.kz", subject="Отчёт", to=ACCOUNT_EMAIL
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg["Date"] = "Mon, 01 Jul 2026 10:00:00 +0000"
    msg.set_content("Тело письма из архива для контекста.")
    return msg.as_bytes()


@pytest.fixture
async def account(db) -> AccountRef:
    async with db.begin() as session:
        user = models.User(tg_user_id=1, tg_chat_id=1)
        session.add(user)
        await session.flush()
        acc = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email=ACCOUNT_EMAIL,
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
        )
        session.add(acc)
        await session.flush()
        return AccountRef(id=acc.id, user_id=user.id, email=ACCOUNT_EMAIL)


async def count(db, model, **where) -> int:
    async with db() as session:
        stmt = select(func.count()).select_from(model)
        for col, val in where.items():
            stmt = stmt.where(getattr(model, col) == val)
        return await session.scalar(stmt)


async def test_archive_indexes_but_does_not_classify(db, account, tmp_path):
    mailbox = FakeArchiveMailbox({10: make_raw("<a@x>"), 11: make_raw("<b@x>", subject="Договор")})

    stats = await import_folder(mailbox, db, account, tmp_path, "INBOX", SINCE)

    assert (stats.seen, stats.saved, stats.skipped_existing) == (2, 2, 0)
    # Индексируются (index_email), но не классифицируются (process_email) и без карточек
    assert await count(db, models.Job, type="index_email") == 2
    assert await count(db, models.Job, type="process_email") == 0
    # История отправителя всё же наполняется — ради неё архив и нужен
    assert await count(db, models.SenderProfile) == 1


async def test_second_run_skips_already_imported_without_fetching_bodies(db, account, tmp_path):
    mailbox = FakeArchiveMailbox({10: make_raw("<a@x>"), 11: make_raw("<b@x>")})
    await import_folder(mailbox, db, account, tmp_path, "INBOX", SINCE)

    mailbox.fetched_bodies.clear()
    stats = await import_folder(mailbox, db, account, tmp_path, "INBOX", SINCE)

    assert (stats.seen, stats.saved, stats.skipped_existing) == (2, 0, 2)
    assert mailbox.fetched_bodies == []  # тела дублей не качаем
    assert await count(db, models.Message) == 2


async def test_existing_message_ids_scoped_to_account(db, account, tmp_path):
    await import_folder(
        FakeArchiveMailbox({10: make_raw("<a@x>")}), db, account, tmp_path, "INBOX", SINCE
    )

    async with db() as session:
        known = await existing_message_ids(session, account.id, ["<a@x>", "<missing@x>"])
    assert known == {"<a@x>"}
