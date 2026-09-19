import asyncio
from email.message import EmailMessage
from pathlib import Path

import pytest
from sqlalchemy import func, select

from mailpulse.db.models import (
    Attachment,
    AuthType,
    Job,
    MailAccount,
    MailboxSync,
    Message,
    Provider,
    SenderProfile,
    Thread,
    User,
)
from mailpulse.mail.sync import AccountRef, FolderState, sync_folder

pytestmark = pytest.mark.db

ACCOUNT_EMAIL = "you@gmail.com"


class FakeMailbox:
    def __init__(self, uidvalidity: int = 1) -> None:
        self.uidvalidity = uidvalidity
        self.messages: dict[int, bytes] = {}

    def deliver(self, raw: bytes) -> int:
        uid = max(self.messages, default=100) + 1
        self.messages[uid] = raw
        return uid

    def select(self, folder: str) -> FolderState:
        return FolderState(self.uidvalidity, max(self.messages, default=100) + 1)

    def uids_after(self, last_uid: int) -> list[int]:
        # Как настоящий IMAP: UID n:* включает последнее письмо, даже если n больше его UID
        newer = [uid for uid in self.messages if uid > last_uid]
        return newer or ([max(self.messages)] if self.messages else [])

    def fetch_raw(self, uids: list[int]) -> dict[int, bytes]:
        return {uid: self.messages[uid] for uid in uids if uid in self.messages}


def make_raw(
    message_id: str,
    *,
    sender: str = "boss@company.kz",
    to: str = ACCOUNT_EMAIL,
    subject: str = "Отчёт",
    in_reply_to: str | None = None,
    attachment: bytes | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content("Пришлите отчёт до пятницы")
    if attachment:
        msg.add_attachment(attachment, maintype="application", subtype="pdf", filename="report.pdf")
    return msg.as_bytes()


@pytest.fixture
async def account(db) -> AccountRef:
    async with db.begin() as session:
        user = User(tg_user_id=1, tg_chat_id=1)
        session.add(user)
        await session.flush()
        mail_account = MailAccount(
            user_id=user.id,
            provider=Provider.GMAIL,
            email=ACCOUNT_EMAIL,
            auth_type=AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
        )
        session.add(mail_account)
        await session.flush()
        return AccountRef(id=mail_account.id, user_id=user.id, email=ACCOUNT_EMAIL)


async def count_process_jobs(db) -> int:
    async with db() as session:
        stmt = select(func.count()).select_from(Job).where(Job.type == "process_email")
        return await session.scalar(stmt)


async def count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_first_sync_sets_baseline_without_processing_archive(db, account, tmp_path):
    mailbox = FakeMailbox()
    mailbox.deliver(make_raw("<old@x>"))

    assert await sync_folder(mailbox, db, account, tmp_path) == 0

    assert await count(db, Message) == 0
    async with db() as session:
        cursor = await session.get(MailboxSync, (account.id, "INBOX"))
    assert (cursor.uidvalidity, cursor.last_uid) == (1, 101)


async def test_new_mail_is_saved_with_job_and_attachment(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.deliver(make_raw("<new@x>", attachment=b"%PDF fake"))

    assert await sync_folder(mailbox, db, account, tmp_path) == 1

    async with db() as session:
        message = (await session.execute(select(Message))).scalar_one()
        job = (await session.execute(select(Job).where(Job.type == "process_email"))).scalar_one()
        index_job = (
            await session.execute(select(Job).where(Job.type == "index_email"))
        ).scalar_one()
        attachment = (await session.execute(select(Attachment))).scalar_one()
    assert message.has_attachments
    assert message.from_addr == "boss@company.kz"
    assert (job.type, job.payload) == ("process_email", {"message_id": message.id})
    assert index_job.payload == {"message_id": message.id}
    assert await asyncio.to_thread(Path(attachment.storage_path).read_bytes) == b"%PDF fake"


async def test_already_seen_uid_is_not_saved_again(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.deliver(make_raw("<one@x>"))
    await sync_folder(mailbox, db, account, tmp_path)

    assert await sync_folder(mailbox, db, account, tmp_path) == 0
    assert await count(db, Message) == 1


async def test_duplicate_message_id_is_saved_once(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.deliver(make_raw("<same@x>"))
    mailbox.deliver(make_raw("<same@x>"))

    assert await sync_folder(mailbox, db, account, tmp_path) == 1
    assert await count_process_jobs(db) == 1


async def test_uidvalidity_change_rebaselines(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.uidvalidity = 2
    mailbox.deliver(make_raw("<after-reset@x>"))

    assert await sync_folder(mailbox, db, account, tmp_path) == 0
    assert await count(db, Message) == 0


async def test_own_messages_are_saved_but_not_processed(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.deliver(make_raw("<mine@x>", sender="You <YOU@gmail.com>"))

    assert await sync_folder(mailbox, db, account, tmp_path) == 1
    assert await count_process_jobs(db) == 0


async def test_reply_chain_forms_one_thread_and_counts_replies(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.deliver(make_raw("<question@x>"))
    mailbox.deliver(
        make_raw(
            "<answer@x>",
            sender=f"You <{ACCOUNT_EMAIL}>",
            to="boss@company.kz",
            subject="Re: Отчёт",
            in_reply_to="<question@x>",
        )
    )
    mailbox.deliver(make_raw("<followup-no-references@x>", subject="RE: отчёт"))

    await sync_folder(mailbox, db, account, tmp_path)

    async with db() as session:
        rows = (
            await session.execute(select(Message.thread_id, Message.outgoing).order_by(Message.id))
        ).all()
        profile = await session.get(SenderProfile, (account.user_id, "boss@company.kz"))
    assert len({thread_id for thread_id, _ in rows}) == 1
    assert [outgoing for _, outgoing in rows] == [False, True, False]
    assert (profile.msg_count, profile.reply_count) == (2, 1)
    assert await count_process_jobs(db) == 2


async def test_same_subject_without_reply_prefix_starts_new_thread(db, account, tmp_path):
    mailbox = FakeMailbox()
    await sync_folder(mailbox, db, account, tmp_path)
    mailbox.deliver(make_raw("<first@x>"))
    mailbox.deliver(make_raw("<second@x>"))

    await sync_folder(mailbox, db, account, tmp_path)

    assert await count(db, Thread) == 2


async def test_copy_in_second_mailbox_is_processed_once(db, account, tmp_path):
    async with db.begin() as session:
        work = MailAccount(
            user_id=account.user_id,
            provider=Provider.IMAP,
            email="work@company.kz",
            auth_type=AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.company.kz",
            smtp_host="smtp.company.kz",
        )
        session.add(work)
        await session.flush()
        work_ref = AccountRef(id=work.id, user_id=account.user_id, email="work@company.kz")

    personal, corporate = FakeMailbox(), FakeMailbox()
    await sync_folder(personal, db, account, tmp_path)
    await sync_folder(corporate, db, work_ref, tmp_path)
    raw = make_raw("<to-both@x>", to=f"{ACCOUNT_EMAIL}, work@company.kz")
    personal.deliver(raw)
    corporate.deliver(raw)

    await sync_folder(personal, db, account, tmp_path)
    await sync_folder(corporate, db, work_ref, tmp_path)

    async with db() as session:
        thread_ids = set((await session.execute(select(Message.thread_id))).scalars())
        profile = await session.get(SenderProfile, (account.user_id, "boss@company.kz"))
    assert await count(db, Message) == 2
    assert await count_process_jobs(db) == 1
    assert len(thread_ids) == 1
    assert profile.msg_count == 1
