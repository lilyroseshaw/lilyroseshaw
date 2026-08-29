"""Encrypt/decrypt the one thing in this app that's actually a secret: the
OAuth refresh token. Everything else in the DB is non-sensitive aggregate data."""
from cryptography.fernet import Fernet

from app import config


def _fernet() -> Fernet:
    config.require_secret_key()
    return Fernet(config.COOKIE_MONSTER_SECRET_KEY.encode())


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    return _fernet().decrypt(ciphertext.encode()).decode()
