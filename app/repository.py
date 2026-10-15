import json
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Admin, ConfigChange, Setting
from app.security import decrypt, encrypt, hash_password, verify_password

SECRET_KEYS = {"integration.test_secret", "snowluma.token", "provider.api_key"}
SNOWLUMA_KEYS = {"snowluma.connection_mode", "snowluma.websocket_url", "snowluma.http_url", "snowluma.token"}
PROVIDER_KEYS = {
    "provider.base_url",
    "provider.api_key",
    "provider.responses_model",
    "provider.embedding_model",
    "provider.embedding_dimensions",
    "provider.timeout_seconds",
    "provider.organization",
    "provider.native_search_enabled",
}
SNOWLUMA_MODES = {"reverse_ws", "forward_ws", "http"}
MASKED_SECRET = "••••••••"
DEFAULT_VALUES = {
    "app.name": ("VireyaBot", False),
    "logging.level": ("INFO", False),
    "features.proactive_enabled": (False, False),
    "integration.test_secret": ("", True),
    "snowluma.connection_mode": ("forward_ws", False),
    "snowluma.websocket_url": ("", False),
    "snowluma.http_url": ("", False),
    "snowluma.token": ("", True),
    "provider.base_url": ("https://api.openai.com/v1", False),
    "provider.api_key": ("", True),
    "provider.responses_model": ("gpt-4.1-mini", False),
    "provider.embedding_model": ("text-embedding-3-small", False),
    "provider.embedding_dimensions": (None, False),
    "provider.timeout_seconds": (30.0, False),
    "provider.organization": ("", False),
    "provider.native_search_enabled": (False, False),
}


def _flatten(data: Mapping, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            result.update(_flatten(value, path))
        elif path in {"app.name", "logging.level", "features.proactive_enabled", *PROVIDER_KEYS}:
            result[path] = value
    return result


def _validate_snowluma(values: Mapping[str, object]) -> None:
    mode = values.get("snowluma.connection_mode")
    websocket_url = values.get("snowluma.websocket_url")
    http_url = values.get("snowluma.http_url")
    if mode not in SNOWLUMA_MODES:
        raise ValueError("snowluma.connection_mode must be reverse_ws, forward_ws, or http")
    if not isinstance(websocket_url, str) or not isinstance(http_url, str):
        raise TypeError("SnowLuma addresses must be strings")
    if mode in {"reverse_ws", "forward_ws"}:
        if not websocket_url:
            raise ValueError("snowluma.websocket_url is required for WebSocket mode")
        if not websocket_url.startswith(("ws://", "wss://")):
            raise ValueError("snowluma.websocket_url must use ws:// or wss://")
        if http_url:
            raise ValueError("snowluma.http_url must be empty in WebSocket mode")
    else:
        if not http_url:
            raise ValueError("snowluma.http_url is required for HTTP mode")
        if not http_url.startswith(("http://", "https://")):
            raise ValueError("snowluma.http_url must use http:// or https://")
        if websocket_url:
            raise ValueError("snowluma.websocket_url must be empty in HTTP mode")


def _legacy_secret_value(stored: str) -> str:
    try:
        value = json.loads(stored)
    except json.JSONDecodeError:
        return stored
    return value if isinstance(value, str) else stored


def _repair_secret_row(row: Setting) -> None:
    if row.is_secret:
        return
    row.value = encrypt(_legacy_secret_value(row.value))
    row.is_secret = True


async def bootstrap(session: AsyncSession) -> None:
    admin = await session.scalar(select(Admin).where(Admin.username == settings.admin_username))
    if admin is None:
        session.add(Admin(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
    defaults = dict(DEFAULT_VALUES)
    for key, value in _flatten(settings.defaults).items():
        if key not in SECRET_KEYS:
            defaults[key] = (value, False)
    for key, (value, is_secret) in defaults.items():
        existing = await session.scalar(select(Setting).where(Setting.key == key))
        if existing is None:
            stored = encrypt(str(value)) if is_secret else json.dumps(value)
            session.add(Setting(key=key, value=stored, is_secret=is_secret))
        elif is_secret:
            _repair_secret_row(existing)
    await session.commit()


async def authenticate(session: AsyncSession, username: str, password: str) -> bool:
    admin = await session.scalar(select(Admin).where(Admin.username == username, Admin.is_active.is_(True)))
    return bool(admin and verify_password(password, admin.password_hash))


async def read_settings(session: AsyncSession) -> dict[str, object]:
    rows = (await session.scalars(select(Setting).order_by(Setting.key))).all()
    result = {}
    for row in rows:
        result[row.key] = MASKED_SECRET if row.is_secret or row.key in SECRET_KEYS else json.loads(row.value)
    return result


async def read_snowluma_settings(session: AsyncSession) -> dict[str, object]:
    rows = (await session.scalars(select(Setting).where(Setting.key.in_(SNOWLUMA_KEYS)))).all()
    result: dict[str, object] = {"connection_mode": "forward_ws", "websocket_url": "", "http_url": "", "token": "", "token_configured": False}
    for row in rows:
        key = row.key.removeprefix("snowluma.")
        if row.is_secret or row.key in SECRET_KEYS:
            if not row.is_secret:
                _repair_secret_row(row)
            result["token_configured"] = bool(decrypt(row.value))
            result["token"] = MASKED_SECRET if result["token_configured"] else ""
        else:
            result[key] = json.loads(row.value)
    return result

async def read_snowluma_runtime_settings(session: AsyncSession) -> dict[str, object]:
    """Read SnowLuma settings for the gateway without exposing secrets to API callers."""
    rows = (await session.scalars(select(Setting).where(Setting.key.in_(SNOWLUMA_KEYS)))).all()
    result: dict[str, object] = {
        "connection_mode": "forward_ws",
        "websocket_url": "",
        "http_url": "",
        "token": "",
        "token_configured": False,
    }
    for row in rows:
        key = row.key.removeprefix("snowluma.")
        if row.is_secret or row.key in SECRET_KEYS:
            result["token"] = decrypt(row.value) if row.is_secret else _legacy_secret_value(row.value)
            result["token_configured"] = bool(result["token"])
        else:
            result[key] = json.loads(row.value)
    return result

async def read_provider_runtime_settings(session: AsyncSession) -> dict[str, object]:
    """Read Provider settings for execution; only this path decrypts the API key."""
    rows = (await session.scalars(select(Setting).where(Setting.key.in_(PROVIDER_KEYS)))).all()
    result: dict[str, object] = {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "responses_model": "gpt-4.1-mini",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": None,
        "timeout_seconds": 30.0,
        "organization": "",
        "native_search_enabled": False,
    }
    for row in rows:
        key = row.key.removeprefix("provider.")
        if row.key in SECRET_KEYS and not row.is_secret:
            _repair_secret_row(row)
        result[key] = decrypt(row.value) if row.is_secret else json.loads(row.value)
    return result



async def save_settings(session: AsyncSession, values: Mapping[str, object], username: str) -> None:
    rows: dict[str, Setting] = {}
    for key in values:
        row = await session.scalar(select(Setting).where(Setting.key == key))
        if row is None or key not in DEFAULT_VALUES:
            raise ValueError(f"Unknown setting: {key}")
        rows[key] = row

    if any(key in SNOWLUMA_KEYS for key in values):
        current = await read_settings(session)
        candidate = {key: current[key] for key in SNOWLUMA_KEYS}
        candidate.update(values)
        if candidate["snowluma.token"] == MASKED_SECRET:
            candidate["snowluma.token"] = current["snowluma.token"]
        _validate_snowluma(candidate)

    for key, value in values.items():
        row = rows[key]
        if key in SECRET_KEYS:
            _repair_secret_row(row)
        if row.is_secret and (value is None or value == MASKED_SECRET or value == ""):
            continue
        if key in {"features.proactive_enabled", "provider.native_search_enabled"} and not isinstance(value, bool):
            raise ValueError(f"{key} must be boolean")
        if key == "provider.embedding_dimensions" and value is not None and not isinstance(value, int):
            raise ValueError("provider.embedding_dimensions must be an integer or null")
        if key == "provider.timeout_seconds" and (not isinstance(value, (int, float)) or value <= 0):
            raise ValueError("provider.timeout_seconds must be positive")
        if key not in {"features.proactive_enabled", "provider.native_search_enabled", "provider.embedding_dimensions", "provider.timeout_seconds"} and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        row.value = encrypt(value) if row.is_secret else json.dumps(value)
        session.add(ConfigChange(key=key, admin_username=username, action="update"))
    await session.commit()


async def save_snowluma_settings(session: AsyncSession, values: Mapping[str, object], username: str) -> None:
    prefixed = {f"snowluma.{key}": value for key, value in values.items()}
    unknown = set(prefixed) - SNOWLUMA_KEYS
    if unknown:
        raise ValueError(f"Unknown SnowLuma setting: {min(unknown)}")
    await save_settings(session, prefixed, username)
