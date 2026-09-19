"""Проверка доступа к Gmail по IMAP с паролем приложения.

Пароль приложения создаётся на https://myaccount.google.com/apppasswords
(нужна включённая двухэтапная аутентификация). Пароль вводится в терминале
и никуда не сохраняется.

    uv run python scripts/check_gmail_imap.py you@gmail.com
"""

import argparse
import getpass
import sys

from imapclient import IMAPClient
from imapclient.exceptions import LoginError

HOST = "imap.gmail.com"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("email")
    args = parser.parse_args()

    password = getpass.getpass(f"Пароль приложения для {args.email}: ").replace(" ", "")
    with IMAPClient(HOST, ssl=True, timeout=30) as client:
        try:
            client.login(args.email, password)
        except LoginError as exc:
            print(f"✗ Вход не удался: {exc}")
            print("  Нужен пароль приложения из 16 символов, а не обычный пароль аккаунта.")
            return 1

        capabilities = {c.decode() for c in client.capabilities()}
        folder = client.select_folder("INBOX", readonly=True)
        print("✓ Вход выполнен")
        print(f"  IDLE (мгновенные уведомления): {'да' if 'IDLE' in capabilities else 'нет'}")
        print(f"  Расширения Gmail X-GM-EXT-1: {'да' if 'X-GM-EXT-1' in capabilities else 'нет'}")
        print(f"  INBOX: {folder[b'EXISTS']} писем, UIDVALIDITY={folder[b'UIDVALIDITY']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
