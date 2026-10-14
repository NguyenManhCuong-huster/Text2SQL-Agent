"""Encryption at rest for secrets kept in the app database (database connection passwords).

The Fernet key is derived from the same secret that signs session cookies (SECRET_KEY or DATA_DIR/.secret),
so a copied app.db alone does not reveal passwords. Changing SECRET_KEY makes stored passwords unreadable;
the affected connections then have to be added again.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from . import auth

PREFIX = "enc1:"
_fernet: Fernet | None = None


class DecryptError(Exception):
    pass


def _box() -> Fernet:
    global _fernet
    if _fernet is None:
        key = hashlib.sha256(b"text2sql/connection-passwords/v1:" + auth._get_secret()).digest()
        _fernet = Fernet(base64.urlsafe_b64encode(key))
    return _fernet


def is_encrypted(value: str) -> bool:
    return value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    return PREFIX + _box().encrypt(plain.encode()).decode()


def decrypt(value: str) -> str:
    if not is_encrypted(value):  # legacy plaintext row (migrated on startup)
        return value
    try:
        return _box().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise DecryptError("The stored password cannot be decrypted (SECRET_KEY changed?). "
                           "Remove the connection and add it again.")
