"""Шифрование секретов почтовых ящиков: пароли приложений и refresh-токены."""

from cryptography.fernet import Fernet

from mailpulse.config import Settings


class SecretsCipher:
    def __init__(self, key: str) -> None:
        self._fernet = Fernet(key.encode())

    @classmethod
    def from_settings(cls, settings: Settings) -> "SecretsCipher":
        if settings.secrets_key is None:
            raise RuntimeError("SECRETS_KEY не задан: сгенерируй ключ через `make secret-key`")
        return cls(settings.secrets_key.get_secret_value())

    def encrypt(self, plaintext: str) -> bytes:
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, token: bytes) -> str:
        return self._fernet.decrypt(token).decode()
