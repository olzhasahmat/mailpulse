from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from mailpulse.db import models
from mailpulse.inject_cli import find_parent, resolve_accounts
from mailpulse.mail.sync import AccountRef, save_message
from mailpulse.testing_templates import all_keys, build_raw, get_template

pytestmark = pytest.mark.db

NOW = datetime(2026, 9, 12, 18, 0, tzinfo=UTC)


@pytest.fixture
async def accounts(db) -> list[AccountRef]:
    async with db.begin() as session:
        user = models.User(tg_user_id=1, tg_chat_id=1)
        session.add(user)
        await session.flush()
        refs = []
        for provider, email, host in (
            (models.Provider.GMAIL, "you@gmail.com", "imap.gmail.com"),
            (models.Provider.IMAP, "you@yandex.ru", "imap.yandex.ru"),
        ):
            account = models.MailAccount(
                user_id=user.id,
                provider=provider,
                email=email,
                auth_type=models.AuthType.APP_PASSWORD,
                secret_enc=b"x",
                imap_host=host,
                smtp_host=host.replace("imap", "smtp"),
            )
            session.add(account)
            await session.flush()
            refs.append(AccountRef(id=account.id, user_id=user.id, email=email))
        return refs


async def inject(db, account: AccountRef, key: str, tmp_path, *, message_id=None, in_reply_to=None):
    raw, msgid = build_raw(
        get_template(key),
        to_addr=account.email,
        message_id=message_id,
        in_reply_to=in_reply_to,
        now=NOW,
    )
    async with db.begin() as session:
        saved = await save_message(session, account, "INBOX", 1, raw, tmp_path, is_test=True)
    return saved, msgid


async def counts(db) -> dict[str, int]:
    async with db() as session:
        return {
            "messages": await session.scalar(select(func.count()).select_from(models.Message)),
            "jobs": await session.scalar(select(func.count()).select_from(models.Job)),
            "process": await session.scalar(
                select(func.count()).where(models.Job.type == "process_email")
            ),
            "index": await session.scalar(
                select(func.count()).where(models.Job.type == "index_email")
            ),
            "chunks": await session.scalar(select(func.count()).select_from(models.Chunk)),
            "senders": await session.scalar(select(func.count()).select_from(models.SenderProfile)),
        }


def test_all_templates_build():
    for key in all_keys():
        raw, msgid = build_raw(get_template(key), to_addr="you@gmail.com", now=NOW)
        assert b"Subject:" in raw
        assert msgid in raw.decode()


async def test_test_email_is_classified_but_not_archived(db, accounts, tmp_path):
    saved, _ = await inject(db, accounts[0], "urgent", tmp_path)

    assert saved
    c = await counts(db)
    # Разбирается (process_email есть), но архив и историю не трогает
    assert c["process"] == 1
    assert c["index"] == 0
    assert c["chunks"] == 0
    assert c["senders"] == 0
    async with db() as session:
        message = (await session.execute(select(models.Message))).scalar_one()
    assert message.is_test


async def test_both_accounts_share_message_id_and_dedupe(db, accounts, tmp_path):
    _, msgid = await inject(db, accounts[0], "invoice", tmp_path)
    saved_copy, _ = await inject(db, accounts[1], "invoice", tmp_path, message_id=msgid)

    assert saved_copy  # копия сохранена…
    c = await counts(db)
    assert c["messages"] == 2
    assert c["process"] == 1  # …но разобрана один раз
    async with db() as session:
        thread_ids = set((await session.execute(select(models.Message.thread_id))).scalars())
    assert len(thread_ids) == 1


async def test_reply_links_to_invoice_thread(db, accounts, tmp_path):
    _, invoice_id = await inject(db, accounts[0], "invoice", tmp_path)
    async with db() as session:
        parent = await find_parent(session, [accounts[0].id], "invoice")
    assert parent == invoice_id

    await inject(db, accounts[0], "reply", tmp_path, in_reply_to=invoice_id)

    async with db() as session:
        threads = list((await session.execute(select(models.Message.thread_id))).scalars())
    assert len(set(threads)) == 1


async def test_resolve_accounts_filters_by_provider(db, accounts):
    async with db() as session:
        gmail = await resolve_accounts(session, "gmail")
        both = await resolve_accounts(session, "both")
    assert [a.email for a in gmail] == ["you@gmail.com"]
    assert len(both) == 2
