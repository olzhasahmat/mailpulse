import pytest
from sqlalchemy import func, select

from mailpulse.agent.adapters.postgres import NO_LLM_META, PostgresMailStore, owner_of_message
from mailpulse.db import models

pytestmark = pytest.mark.db


@pytest.fixture
async def message_id(db) -> int:
    async with db.begin() as session:
        user = models.User(tg_user_id=555, tg_chat_id=555)
        session.add(user)
        await session.flush()
        account = models.MailAccount(
            user_id=user.id,
            provider=models.Provider.GMAIL,
            email="you@gmail.com",
            auth_type=models.AuthType.APP_PASSWORD,
            secret_enc=b"x",
            imap_host="imap.gmail.com",
            smtp_host="smtp.gmail.com",
        )
        session.add(account)
        await session.flush()
        message = models.Message(
            account_id=account.id,
            folder="INBOX",
            uid=1,
            message_id_hdr="<m@x>",
            from_addr="a.seitkali@partner.kz",
            subject="Счёт",
            body_text="Во вложении счёт",
            headers={"list-unsubscribe": "<mailto:x>"},
        )
        session.add(message)
        await session.flush()
        return message.id


def triage(importance: int) -> dict:
    return {
        "reasoning": "r",
        "importance": importance,
        "category": "finance",
        "needs_reply": False,
        "suspicious": False,
        "extracted": {},
    }


async def test_load_email_and_owner(db, message_id):
    email = await PostgresMailStore(db).load_email(message_id)
    async with db() as session:
        owner = await owner_of_message(session, message_id)

    assert email["from_addr"] == "a.seitkali@partner.kz"
    assert email["headers"] == {"list-unsubscribe": "<mailto:x>"}
    assert owner.tg_user_id == 555


async def test_rules_match_address_or_domain_and_vip_wins(db, message_id):
    store = PostgresMailStore(db)
    async with db() as session:
        user_id = (await owner_of_message(session, message_id)).id

    assert await store.rule_for_sender(user_id, "a.seitkali@partner.kz") is None

    async with db.begin() as session:
        session.add(
            models.UserRule(user_id=user_id, kind=models.RuleKind.MUTE, value="@partner.kz")
        )
    assert await store.rule_for_sender(user_id, "A.Seitkali@partner.kz") == "mute"

    async with db.begin() as session:
        session.add(
            models.UserRule(
                user_id=user_id, kind=models.RuleKind.VIP, value="a.seitkali@partner.kz"
            )
        )
    assert await store.rule_for_sender(user_id, "a.seitkali@partner.kz") == "vip"


async def test_save_triage_is_idempotent(db, message_id):
    store = PostgresMailStore(db)

    await store.save_triage(message_id, triage(1))
    await store.save_triage(
        message_id, {**triage(3), "meta": {**NO_LLM_META, "model": "claude-haiku-4-5"}}
    )

    async with db() as session:
        saved = await session.get(models.TriageResult, message_id)
    assert (saved.importance, saved.model) == (3, "claude-haiku-4-5")


async def test_drafts_feedback_and_usage(db, message_id):
    store = PostgresMailStore(db)
    async with db() as session:
        user_id = (await owner_of_message(session, message_id)).id

    draft_id = await store.save_draft(message_id, "Спасибо, оплатим", version=1)
    await store.approve_draft(draft_id)
    await store.save_feedback(user_id, message_id, "importance_wrong", {"importance": 3})
    await store.record_llm_usage(user_id, "classify", {**NO_LLM_META, "cost_usd": 0.0015})

    async with db() as session:
        draft = await session.get(models.Draft, draft_id)
        feedback_count = await session.scalar(select(func.count()).select_from(models.Feedback))
        cost = await session.scalar(select(func.sum(models.LlmUsage.cost_usd)))
    assert draft.status == models.DraftStatus.APPROVED
    assert feedback_count == 1
    assert float(cost) == 0.0015


async def test_sender_profile(db, message_id):
    store = PostgresMailStore(db)
    async with db() as session:
        user_id = (await owner_of_message(session, message_id)).id

    assert await store.sender_profile(user_id, "boss@company.kz") is None

    async with db.begin() as session:
        session.add(
            models.SenderProfile(
                user_id=user_id, address="boss@company.kz", msg_count=3, reply_count=2
            )
        )
    profile = await store.sender_profile(user_id, "Boss@Company.kz")

    assert (profile["msg_count"], profile["reply_count"]) == (3, 2)
