"""Тексты и ссылки для уведомлений пользователю в Telegram (вне графа обработки писем)."""

from mailpulse.db.models import MailAccount

# домен -> (название сервиса, страница управления паролями приложения)
APP_PASSWORD_URLS = {
    "gmail.com": ("Gmail", "https://myaccount.google.com/apppasswords"),
    "googlemail.com": ("Gmail", "https://myaccount.google.com/apppasswords"),
    "yandex.ru": ("Яндекс", "https://id.yandex.ru/security/app-passwords"),
    "yandex.kz": ("Яндекс", "https://id.yandex.ru/security/app-passwords"),
    "ya.ru": ("Яндекс", "https://id.yandex.ru/security/app-passwords"),
    "mail.ru": ("Mail.ru", "https://account.mail.ru/user/2-step-auth/passwords/"),
}


def _app_password_link(email: str) -> tuple[str, str] | None:
    return APP_PASSWORD_URLS.get(email.rpartition("@")[2].casefold())


def account_deleted_message(email: str) -> str:
    """Сообщение после удаления ящика: удаление не отзывает пароль приложения на стороне почты."""
    link = _app_password_link(email)
    if link:
        service, url = link
        revoke = f'<a href="{url}">🔑 Отозвать пароль приложения в {service}</a>'
    else:
        revoke = "Отзовите пароль приложения в настройках безопасности вашего почтового сервиса."
    return (
        f"⚠️ <b>Важно: отзовите пароль приложения</b>\n\n"
        f"Ящик <b>{email}</b> и все его данные удалены из MailPulse. "
        f"Но пароль приложения <b>всё ещё действует</b> на стороне почтового сервиса — "
        f"пока вы его не отзовёте, по нему можно зайти в вашу почту.\n\n"
        f"<b>Отзовите его прямо сейчас:</b>\n{revoke}\n\n"
        f"После отзыва доступ будет полностью закрыт."
    )


def account_deleted_message_for(account: MailAccount) -> str:
    return account_deleted_message(account.email)
