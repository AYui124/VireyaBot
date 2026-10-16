import json
import uuid
from collections.abc import Mapping
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Admin, ConfigChange, Provider, ProviderModelConfig, ProviderSecret, Setting
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
    defaults = {key: value for key, value in DEFAULT_VALUES.items() if key not in PROVIDER_KEYS}
    for key, value in _flatten(settings.defaults).items():
        if key not in SECRET_KEYS and key not in PROVIDER_KEYS:
            defaults[key] = (value, False)
    for key, (value, is_secret) in defaults.items():
        existing = await session.scalar(select(Setting).where(Setting.key == key))
        if existing is None:
            stored = encrypt(str(value)) if is_secret else json.dumps(value)
            session.add(Setting(key=key, value=stored, is_secret=is_secret))
        elif is_secret:
            _repair_secret_row(existing)
    await session.commit()
    await _migrate_legacy_provider(session)


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

def _validate_provider_values(values: Mapping[str, object]) -> None:
    protocol_type = values.get("protocol_type")
    base_url = values.get("base_url")
    timeout_seconds = values.get("timeout_seconds")
    dimensions = values.get("embedding_dimensions")
    if protocol_type != "openai_responses":
        raise ValueError("protocol_type must be openai_responses")
    if not isinstance(base_url, str) or urlparse(base_url).scheme not in {"http", "https"} or not urlparse(base_url).netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0 or timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 0 and 600")
    if dimensions is not None and (not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0):
        raise ValueError("embedding_dimensions must be a positive integer or null")
    for key in ("name", "responses_model", "embedding_model"):
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise ValueError(f"{key} must not be empty")
    if not isinstance(values.get("organization", ""), str):
        raise TypeError("organization must be a string")
    if not isinstance(values.get("enabled"), bool):
        raise TypeError("enabled must be boolean")


def _provider_public(provider: Provider, secret: ProviderSecret | None, model: ProviderModelConfig | None) -> dict[str, object]:
    return {
        "providerId": provider.provider_id,
        "name": provider.name,
        "protocolType": provider.protocol_type,
        "baseUrl": provider.base_url,
        "timeoutSeconds": provider.timeout_seconds,
        "organization": provider.organization,
        "enabled": provider.enabled,
        "apiKeyConfigured": bool(secret and decrypt(secret.api_key)),
        "responsesModel": model.responses_model if model else "",
        "embeddingModel": model.embedding_model if model else "",
        "embeddingDimensions": model.embedding_dimensions if model else None,
        "nativeSearchEnabled": bool(model and model.native_search_enabled),
    }


async def _provider_rows(session: AsyncSession) -> tuple[list[Provider], dict[int, ProviderSecret], dict[int, ProviderModelConfig]]:
    providers = list((await session.scalars(select(Provider).order_by(Provider.created_at, Provider.provider_id))).all())
    ids = [provider.id for provider in providers]
    secrets = list((await session.scalars(select(ProviderSecret).where(ProviderSecret.provider_id.in_(ids)))).all()) if ids else []
    models = list((await session.scalars(select(ProviderModelConfig).where(ProviderModelConfig.provider_id.in_(ids)))).all()) if ids else []
    return providers, {item.provider_id: item for item in secrets}, {item.provider_id: item for item in models}


async def list_provider_configs(session: AsyncSession) -> list[dict[str, object]]:
    providers, secrets, models = await _provider_rows(session)
    return [_provider_public(provider, secrets.get(provider.id), models.get(provider.id)) for provider in providers]


async def _migrate_legacy_provider(session: AsyncSession) -> None:
    if await session.scalar(select(Provider).limit(1)) is not None:
        return
    rows = list((await session.scalars(select(Setting).where(Setting.key.in_(PROVIDER_KEYS)))).all())
    if not rows:
        return
    values: dict[str, object] = {
        "base_url": "https://api.openai.com/v1",
        "responses_model": "gpt-4.1-mini",
        "embedding_model": "text-embedding-3-small",
        "embedding_dimensions": None,
        "timeout_seconds": 30.0,
        "organization": "",
        "native_search_enabled": False,
        "api_key": "",
    }
    for row in rows:
        key = row.key.removeprefix("provider.")
        if key == "api_key" and not row.is_secret:
            _repair_secret_row(row)
        values[key] = decrypt(row.value) if row.is_secret else json.loads(row.value)
    provider_id = "provider-" + uuid.uuid5(uuid.NAMESPACE_URL, "vireyabot/provider/legacy-default").hex[:24]
    candidate = {"name": "默认 Provider", "protocol_type": "openai_responses", "enabled": True, **values}
    _validate_provider_values(candidate)
    provider = Provider(provider_id=provider_id, name=candidate["name"], protocol_type=candidate["protocol_type"], base_url=candidate["base_url"], timeout_seconds=candidate["timeout_seconds"], organization=candidate["organization"], enabled=True)
    session.add(provider)
    await session.flush()
    session.add(ProviderSecret(provider_id=provider.id, api_key=encrypt(str(values["api_key"]))))
    session.add(ProviderModelConfig(provider_id=provider.id, responses_model=values["responses_model"], embedding_model=values["embedding_model"], embedding_dimensions=values["embedding_dimensions"], native_search_enabled=values["native_search_enabled"]))
    for row in rows:
        await session.delete(row)
    await session.commit()


async def read_provider_runtime_settings(session: AsyncSession, provider_id: str) -> dict[str, object]:
    """Resolve one enabled Provider by stable ID; never falls back to another resource."""
    provider = await session.scalar(select(Provider).where(Provider.provider_id == provider_id))
    if provider is None:
        raise ValueError("Unknown providerId")
    if not provider.enabled:
        raise ValueError("Provider is disabled")
    secret = await session.scalar(select(ProviderSecret).where(ProviderSecret.provider_id == provider.id))
    model = await session.scalar(select(ProviderModelConfig).where(ProviderModelConfig.provider_id == provider.id))
    if secret is None or model is None:
        raise ValueError("Provider configuration is incomplete")
    return {
        "base_url": provider.base_url,
        "api_key": decrypt(secret.api_key),
        "responses_model": model.responses_model,
        "embedding_model": model.embedding_model,
        "embedding_dimensions": model.embedding_dimensions,
        "timeout_seconds": provider.timeout_seconds,
        "organization": provider.organization,
        "native_search_enabled": model.native_search_enabled,
    }


def _new_provider_id() -> str:
    return "provider-" + uuid.uuid4().hex[:24]


async def create_provider_config(session: AsyncSession, values: Mapping[str, object], username: str) -> dict[str, object]:
    candidate = {"name": values.get("name"), "protocol_type": values.get("protocol_type", "openai_responses"), "base_url": values.get("base_url"), "timeout_seconds": values.get("timeout_seconds"), "organization": values.get("organization", ""), "enabled": values.get("enabled", True), "responses_model": values.get("responses_model"), "embedding_model": values.get("embedding_model"), "embedding_dimensions": values.get("embedding_dimensions"), "native_search_enabled": values.get("native_search_enabled", False)}
    _validate_provider_values(candidate)
    provider = Provider(provider_id=_new_provider_id(), name=candidate["name"], protocol_type=candidate["protocol_type"], base_url=candidate["base_url"], timeout_seconds=candidate["timeout_seconds"], organization=candidate["organization"], enabled=candidate["enabled"])
    session.add(provider)
    await session.flush()
    session.add(ProviderSecret(provider_id=provider.id, api_key=encrypt(str(values.get("api_key") or ""))))
    model = ProviderModelConfig(provider_id=provider.id, responses_model=candidate["responses_model"], embedding_model=candidate["embedding_model"], embedding_dimensions=candidate["embedding_dimensions"], native_search_enabled=candidate["native_search_enabled"])
    session.add(model)
    session.add(ConfigChange(key=f"provider:{provider.provider_id}", admin_username=username, action="create"))
    await session.commit()
    return _provider_public(provider, await session.scalar(select(ProviderSecret).where(ProviderSecret.provider_id == provider.id)), model)


async def update_provider_config(session: AsyncSession, provider_id: str, values: Mapping[str, object], username: str) -> dict[str, object]:
    provider = await session.scalar(select(Provider).where(Provider.provider_id == provider_id))
    if provider is None:
        raise ValueError("Unknown providerId")
    secret = await session.scalar(select(ProviderSecret).where(ProviderSecret.provider_id == provider.id))
    model = await session.scalar(select(ProviderModelConfig).where(ProviderModelConfig.provider_id == provider.id))
    if secret is None or model is None:
        raise ValueError("Provider configuration is incomplete")
    candidate = {"name": values.get("name", provider.name), "protocol_type": values.get("protocol_type", provider.protocol_type), "base_url": values.get("base_url", provider.base_url), "timeout_seconds": values.get("timeout_seconds", provider.timeout_seconds), "organization": values.get("organization", provider.organization), "enabled": values.get("enabled", provider.enabled), "responses_model": values.get("responses_model", model.responses_model), "embedding_model": values.get("embedding_model", model.embedding_model), "embedding_dimensions": values.get("embedding_dimensions", model.embedding_dimensions), "native_search_enabled": values.get("native_search_enabled", model.native_search_enabled)}
    _validate_provider_values(candidate)
    provider.name = candidate["name"]
    provider.protocol_type = candidate["protocol_type"]
    provider.base_url = candidate["base_url"]
    provider.timeout_seconds = candidate["timeout_seconds"]
    provider.organization = candidate["organization"]
    provider.enabled = candidate["enabled"]
    model.responses_model = candidate["responses_model"]
    model.embedding_model = candidate["embedding_model"]
    model.embedding_dimensions = candidate["embedding_dimensions"]
    model.native_search_enabled = candidate["native_search_enabled"]
    api_key = values.get("api_key")
    if api_key is not None and api_key not in {"", MASKED_SECRET}:
        secret.api_key = encrypt(str(api_key))
    session.add(ConfigChange(key=f"provider:{provider_id}", admin_username=username, action="update"))
    await session.commit()
    return _provider_public(provider, secret, model)


async def set_provider_enabled(session: AsyncSession, provider_id: str, enabled: bool, username: str) -> dict[str, object]:
    return await update_provider_config(session, provider_id, {"enabled": enabled}, username)





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
