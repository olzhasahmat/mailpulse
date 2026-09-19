"""Профили отправителей: сколько писем пришло с адреса и на сколько из них пользователь ответил."""

from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mailpulse.db.models import SenderProfile


async def record_contact(
    session: AsyncSession,
    *,
    user_id: int,
    own_address: str,
    sender: str,
    recipients: Iterable[str],
    outgoing: bool,
    is_reply: bool,
    at: datetime,
) -> None:
    if outgoing:
        # Ответ пользователя засчитывается всем адресатам ответа
        increments = {address: (0, int(is_reply)) for address in recipients}
    else:
        increments = {sender: (1, 0)}
    rows = [
        {
            "user_id": user_id,
            "address": address,
            "msg_count": msg_count,
            "reply_count": reply_count,
            "last_contact_at": at,
        }
        for address, (msg_count, reply_count) in increments.items()
        if address and address != own_address
    ]
    if not rows:
        return

    stmt = insert(SenderProfile).values(rows)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[SenderProfile.user_id, SenderProfile.address],
            set_={
                "msg_count": SenderProfile.msg_count + stmt.excluded.msg_count,
                "reply_count": SenderProfile.reply_count + stmt.excluded.reply_count,
                "last_contact_at": func.greatest(
                    SenderProfile.last_contact_at, stmt.excluded.last_contact_at
                ),
                "updated_at": func.now(),
            },
        )
    )
