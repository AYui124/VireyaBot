import base64
import hashlib
import hmac
import secrets
import time

from cryptography.fernet import Fernet

from app.config import settings


def _fernet() -> Fernet:
    try:
        return Fernet(settings.config_master_key.encode())
    except Exception as exc:
        raise RuntimeError("CONFIG_MASTER_KEY must be a valid Fernet key") from exc


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        method, rounds, salt_b64, digest_b64 = encoded.split("$", 3)
        if method != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(salt_b64.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(rounds))
        return hmac.compare_digest(actual, base64.urlsafe_b64decode(digest_b64.encode()))
    except (ValueError, TypeError):
        return False


def make_session(username: str) -> str:
    issued = str(int(time.time()))
    payload = f"{username}:{issued}"
    key = settings.config_master_key.encode()
    signature = hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def session_username(token: str | None, max_age: int = 86_400) -> str | None:
    if not token:
        return None
    try:
        username, issued, signature = token.rsplit(":", 2)
        payload = f"{username}:{issued}"
        expected = hmac.new(settings.config_master_key.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(time.time()) - int(issued) > max_age:
            return None
        return username
    except (ValueError, TypeError):
        return None
