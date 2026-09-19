"""Проверка корпоративного ящика Microsoft 365: IMAP и SMTP через OAuth2 (XOAUTH2).

Заранее нужна регистрация приложения в Microsoft Entra ID (portal.azure.com → App registrations):
  1. Supported account types: Accounts in any organizational directory (multi-tenant).
  2. Authentication → Allow public client flows: Yes (для входа по коду устройства).
  3. API permissions (delegated): IMAP.AccessAsUser.All и SMTP.Send.
     Если не добавить, разрешения запросятся при входе.
Client ID приложения положи в MICROSOFT_CLIENT_ID в .env.

    make check-microsoft email=you@company.com
"""

import argparse
import base64
import os
import smtplib
import sys

import msal
from imapclient import IMAPClient

SCOPES = [
    "https://outlook.office.com/IMAP.AccessAsUser.All",
    "https://outlook.office.com/SMTP.Send",
]
IMAP_HOST = "outlook.office365.com"
SMTP_HOST = "smtp.office365.com"

# Коды ошибок входа, которые означают политику компании, а не ошибку в коде
AAD_HINTS = {
    "AADSTS65001": "согласие на доступ не дано: повтори вход и прими запрос разрешений",
    "AADSTS90094": "нужно согласие администратора: в компании запрещено давать доступ "
    "сторонним приложениям",
    "AADSTS50105": "администратор ограничил, кому доступно приложение",
    "AADSTS53003": "вход заблокирован политикой условного доступа (Conditional Access)",
    "AADSTS700016": "приложение не найдено: проверь MICROSOFT_CLIENT_ID и что оно multi-tenant",
    "AADSTS7000218": "в регистрации приложения не включено Allow public client flows",
}


def get_token(client_id: str, tenant: str) -> str | None:
    app = msal.PublicClientApplication(
        client_id, authority=f"https://login.microsoftonline.com/{tenant}"
    )
    flow = app.initiate_device_flow(scopes=SCOPES)
    if "user_code" not in flow:
        print(f"✗ Не удалось начать вход: {flow.get('error_description', flow)}")
        return None
    print(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" in result:
        return result["access_token"]

    description = result.get("error_description", "")
    print(f"✗ Токен не получен: {result.get('error')}")
    if description:
        print(f"  {description.splitlines()[0]}")
    for code, hint in AAD_HINTS.items():
        if code in description:
            print(f"  → {hint}")
    return None


def check_imap(email: str, token: str) -> bool:
    try:
        with IMAPClient(IMAP_HOST, ssl=True, timeout=30) as client:
            client.oauth2_login(email, token)
            capabilities = {c.decode() for c in client.capabilities()}
            folder = client.select_folder("INBOX", readonly=True)
    except Exception as exc:
        print(f"✗ IMAP: {exc}")
        print("  → вероятно, администратор выключил IMAP для ящика (Set-CASMailbox -ImapEnabled)")
        return False
    print("✓ IMAP: вход выполнен")
    print(f"  IDLE: {'да' if 'IDLE' in capabilities else 'нет'}")
    print(f"  INBOX: {folder[b'EXISTS']} писем")
    return True


def check_smtp(email: str, token: str) -> bool:
    auth = base64.b64encode(f"user={email}\x01auth=Bearer {token}\x01\x01".encode()).decode()
    with smtplib.SMTP(SMTP_HOST, 587, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        code, response = smtp.docmd("AUTH", f"XOAUTH2 {auth}")
        if code == 334:
            # Сервер прислал детали ошибки; пустая строка завершает обмен и даёт итоговый код
            code, response = smtp.docmd("")
    if code == 235:
        print("✓ SMTP: вход выполнен, отправка ответов возможна")
        return True
    text = response.decode(errors="replace")
    print(f"✗ SMTP: {code} {text}")
    if "SmtpClientAuthentication" in text:
        print("  → SMTP AUTH выключен: читать почту можно, отправлять ответы через SMTP нельзя")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("email")
    parser.add_argument("--client-id", default=os.environ.get("MICROSOFT_CLIENT_ID"))
    parser.add_argument("--tenant", default=os.environ.get("MICROSOFT_TENANT", "organizations"))
    args = parser.parse_args()
    if not args.client_id:
        print("✗ Нужен Client ID: MICROSOFT_CLIENT_ID в .env или --client-id")
        return 2

    token = get_token(args.client_id, args.tenant)
    if token is None:
        print("\nИтог: вход не разрешён политикой компании или настройками приложения.")
        return 1

    imap_ok = check_imap(args.email, token)
    smtp_ok = check_smtp(args.email, token)
    if imap_ok and smtp_ok:
        print("\nИтог: ящик подходит полностью — чтение и отправка.")
    elif imap_ok:
        print(
            "\nИтог: чтение работает, отправки через SMTP нет — "
            "ответы из бота для этого ящика выключаем."
        )
    else:
        print("\nИтог: по IMAP этот ящик не подключить — вторым ящиком берём Яндекс или Mail.ru.")
    return 0 if imap_ok else 1


if __name__ == "__main__":
    sys.exit(main())
