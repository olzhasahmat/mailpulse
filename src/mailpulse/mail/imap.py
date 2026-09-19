"""IMAP через imapclient. Клиент блокирующий: вызывается из потоков через asyncio.to_thread."""

import contextlib
import threading
import time
from dataclasses import dataclass, field
from datetime import date

from imapclient import IMAPClient

from mailpulse.db.models import AuthType
from mailpulse.mail.sync import FolderState

SOCKET_TIMEOUT_S = 60
IDLE_POLL_S = 5
# Флаг папки отправленных (RFC 6154): у Gmail её имя зависит от языка интерфейса
SENT_FLAG = b"\\Sent"


@dataclass(frozen=True)
class ImapCredentials:
    host: str
    port: int
    email: str
    auth_type: AuthType
    secret: str = field(repr=False)


class ImapMailbox:
    def __init__(self, client: IMAPClient, stop: threading.Event) -> None:
        self._client = client
        self._stop = stop

    @classmethod
    def connect(cls, creds: ImapCredentials, stop: threading.Event) -> "ImapMailbox":
        client = IMAPClient(creds.host, port=creds.port, ssl=True, timeout=SOCKET_TIMEOUT_S)
        try:
            if creds.auth_type is not AuthType.APP_PASSWORD:
                raise NotImplementedError("вход через OAuth2 (Microsoft 365) появится на день 4")
            client.login(creds.email, creds.secret)
        except BaseException:
            client.shutdown()
            raise
        return cls(client, stop)

    def select(self, folder: str) -> FolderState:
        # readonly: EXAMINE не меняет флаги, письма не становятся прочитанными
        info = self._client.select_folder(folder, readonly=True)
        uidnext = info.get(b"UIDNEXT")
        if uidnext is None:
            uidnext = max(self._client.search("ALL"), default=0) + 1
        return FolderState(uidvalidity=int(info[b"UIDVALIDITY"]), uidnext=int(uidnext))

    def sent_folder(self) -> str | None:
        return self._client.find_special_folder(SENT_FLAG)

    def uids_after(self, last_uid: int) -> list[int]:
        return list(self._client.search(["UID", f"{last_uid + 1}:*"]))

    def uids_since(self, since: date) -> list[int]:
        """UID писем, полученных не раньше since. Для импорта архива идём в прошлое от курсора."""
        return sorted(self._client.search(["SINCE", since]))

    def message_ids(self, uids: list[int]) -> dict[int, str | None]:
        """Message-ID по ENVELOPE — дешевле полного письма, чтобы отсеять уже сохранённые."""
        response = self._client.fetch(uids, ["ENVELOPE"])
        result: dict[int, str | None] = {}
        for uid, data in response.items():
            envelope = data.get(b"ENVELOPE")
            raw_id = getattr(envelope, "message_id", None) if envelope else None
            result[uid] = raw_id.decode(errors="replace") if raw_id else None
        return result

    def fetch_raw(self, uids: list[int]) -> dict[int, bytes]:
        response = self._client.fetch(uids, ["BODY.PEEK[]"])
        return {uid: data[b"BODY[]"] for uid, data in response.items() if b"BODY[]" in data}

    def wait_for_changes(self, max_wait_s: float) -> bool:
        """IMAP IDLE: ждёт изменений в папке, но не дольше max_wait_s — серверы рвут долгий IDLE."""
        deadline = time.monotonic() + max_wait_s
        self._client.idle()
        try:
            while time.monotonic() < deadline and not self._stop.is_set():
                if self._client.idle_check(timeout=IDLE_POLL_S):
                    return True
            return False
        finally:
            self._client.idle_done()

    def close(self) -> None:
        try:
            self._client.logout()
        except Exception:
            with contextlib.suppress(Exception):
                self._client.shutdown()
