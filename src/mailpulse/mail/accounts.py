"""Почтовые ящики: пресеты серверов, сохранение с шифрованием секрета, выборки для приёма почты."""

from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from mailpulse.db.models import AccountStatus, AuthType, MailAccount, Provider
from mailpulse.mail.imap import ImapCredentials
from mailpulse.mail.sync import AccountRef
from mailpulse.security import SecretsCipher


@dataclass(frozen=True)
class ServerPreset:
    provider: Provider
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int


GMAIL = ServerPreset(Provider.GMAIL, "imap.gmail.com", 993, "smtp.gmail.com", 587)
MICROSOFT = ServerPreset(
    Provider.MICROSOFT, "outlook.office365.com", 993, "smtp.office365.com", 587
)
YANDEX = ServerPreset(Provider.IMAP, "imap.yandex.ru", 993, "smtp.yandex.ru", 465)

PRESETS_BY_PROVIDER = {Provider.GMAIL: GMAIL, Provider.MICROSOFT: MICROSOFT}
PRESETS_BY_DOMAIN = {
    "gmail.com": GMAIL,
    "googlemail.com": GMAIL,
    "yandex.ru": YANDEX,
    "yandex.kz": YANDEX,
    "yandex.com": YANDEX,
    "ya.ru": YANDEX,
}


def preset_for(email: str, provider: Provider | None = None) -> ServerPreset | None:
    if provider in PRESETS_BY_PROVIDER:
        return PRESETS_BY_PROVIDER[provider]
    return PRESETS_BY_DOMAIN.get(email.rpartition("@")[2].casefold())


async def upsert_account(
    session: AsyncSession,
    *,
    user_id: int,
    email: str,
    preset: ServerPreset,
    auth_type: AuthType,
    secret_enc: bytes,
) -> int:
    values = {
        "provider": preset.provider,
        "auth_type": auth_type,
        "secret_enc": secret_enc,
        "imap_host": preset.imap_host,
        "imap_port": preset.imap_port,
        "smtp_host": preset.smtp_host,
        "smtp_port": preset.smtp_port,
        "status": AccountStatus.ACTIVE,
        "last_error": None,
    }
    stmt = (
        insert(MailAccount)
        .values(user_id=user_id, email=email.casefold(), **values)
        .on_conflict_do_update(index_elements=[MailAccount.user_id, MailAccount.email], set_=values)
        .returning(MailAccount.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def active_account_ids(session: AsyncSession) -> set[int]:
    stmt = select(MailAccount.id).where(MailAccount.status == AccountStatus.ACTIVE)
    return set((await session.execute(stmt)).scalars())


async def load_credentials(
    session: AsyncSession, cipher: SecretsCipher, account_id: int
) -> tuple[AccountRef, ImapCredentials] | None:
    account = await session.get(MailAccount, account_id)
    if account is None or account.status != AccountStatus.ACTIVE:
        return None
    ref = AccountRef(id=account.id, user_id=account.user_id, email=account.email)
    creds = ImapCredentials(
        host=account.imap_host,
        port=account.imap_port,
        email=account.email,
        auth_type=account.auth_type,
        secret=cipher.decrypt(account.secret_enc),
    )
    return ref, creds


async def set_status(
    session: AsyncSession, account_id: int, status: AccountStatus, error: str | None = None
) -> None:
    stmt = (
        update(MailAccount)
        .where(MailAccount.id == account_id)
        .values(status=status, last_error=error[:2000] if error else None)
    )
    await session.execute(stmt)
