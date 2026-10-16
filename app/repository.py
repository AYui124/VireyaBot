import json
import math
import uuid
from collections.abc import Mapping
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Admin, ConfigChange, Provider, ProviderModelConfig, ProviderSecret, Setting
from app.provider_settings import PROVIDER_PROTOCOLS, HeaderRow
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

def _validate_provider_secret(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError("api_key must be a string")
    if len(value) > 4096:
        raise ValueError("api_key exceeds the maximum permitted length")

def _validate_provider_runtime_options(values: Mapping[str, object]) -> None:
    default_stream = values.get("default_stream")
    temperature = values.get("temperature")
    max_output_tokens = values.get("max_output_tokens")
    if not isinstance(default_stream, bool):
        raise TypeError("default_stream must be boolean")
    if not isinstance(max_output_tokens, int) or isinstance(max_output_tokens, bool) or not 1 <= max_output_tokens <= 32768:
        raise ValueError("max_output_tokens must be between 1 and 32768")
    if temperature is not None and (not isinstance(temperature, (int, float)) or isinstance(temperature, bool) or not math.isfinite(temperature) or not 0 <= temperature <= 2):
        raise ValueError("temperature must be between 0 and 2")


def _validate_provider_values(values: Mapping[str, object]) -> None:
    protocol_type = values.get("protocol_type")
    base_url = values.get("base_url")
    timeout_seconds = values.get("timeout_seconds")
    dimensions = values.get("embedding_dimensions")
    parsed_url = urlparse(base_url) if isinstance(base_url, str) else None
    if protocol_type not in {"openai_responses", "openai_chat_completions", "anthropic_messages"}:
        raise ValueError("protocol_type is unsupported")
    if parsed_url is None or parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError("base_url must be an absolute http(s) URL")
    if parsed_url.username is not None or parsed_url.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parsed_url.query or parsed_url.fragment or parsed_url.path.rstrip("/").endswith(("/responses", "/chat/completions", "/messages", "/embeddings")):
        raise ValueError("base_url must be an SDK base URL without query, fragment or endpoint suffix")
    try:
        port = parsed_url.port
    except ValueError:
        raise ValueError("base_url contains an invalid port") from None
    if port == 0:
        raise ValueError("base_url contains an invalid port")
    if not parsed_url.hostname or any(char.isspace() for char in base_url):
        raise ValueError("base_url contains an invalid host")
    if protocol_type == "anthropic_messages":
        if values.get("organization"):
            raise ValueError("organization is unsupported for anthropic_messages")
        if values.get("temperature") is not None and values["temperature"] > 1:
            raise ValueError("anthropic temperature must be between 0 and 1")
    if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or timeout_seconds > 600:
        raise ValueError("timeout_seconds must be between 0 and 600")
    if dimensions is not None and (not isinstance(dimensions, int) or isinstance(dimensions, bool) or dimensions <= 0):
        raise ValueError("embedding_dimensions must be a positive integer or null")
    required_text = ("name", "responses_model")
    if protocol_type != "anthropic_messages":
        required_text = (*required_text, "embedding_model")
    for key in required_text:
        if not isinstance(values.get(key), str) or not values[key].strip():
            raise ValueError(f"{key} must not be empty")
    if protocol_type == "anthropic_messages" and values.get("native_search_enabled"):
        raise ValueError("native_search_enabled is unsupported for anthropic_messages")
    if protocol_type != "openai_responses" and values.get("native_search_enabled"):
        raise ValueError("native_search_enabled is unsupported for this protocol")
    for field in ("organization", "project"):
        if not isinstance(values.get(field, ""), str):
            raise TypeError(f"{field} must be a string")
    if not isinstance(values.get("enabled"), bool):
        raise TypeError("enabled must be boolean")
    if not isinstance(values.get("native_search_enabled"), bool):
        raise TypeError("native_search_enabled must be boolean")




def _provider_public(provider: Provider, secret: ProviderSecret | None, model: ProviderModelConfig | None) -> dict[str, object]:
    anthropic = provider.protocol_type == "anthropic_messages"
    headers = _read_provider_headers(secret)
    return {
        "providerId": provider.provider_id,
        "name": provider.name,
        "protocolType": provider.protocol_type,
        "baseUrl": provider.base_url,
        "timeoutSeconds": provider.timeout_seconds,
        "organization": provider.organization,
        "project": provider.project,
        "enabled": provider.enabled,
        "apiKeyConfigured": bool(secret and secret.api_key and decrypt(secret.api_key)),
        "responsesModel": model.responses_model if model else "",
        "embeddingModel": "" if anthropic else (model.embedding_model if model else ""),
        "embeddingDimensions": None if anthropic else (model.embedding_dimensions if model else None),
        "nativeSearchEnabled": False if anthropic else bool(model and model.native_search_enabled),
        "defaultStream": bool(model and model.default_stream),
        "temperature": model.temperature if model else None,
        "maxOutputTokens": model.max_output_tokens if model else 4096,
        "extraHeadersConfigured": bool(headers),
        "extraHeaderNames": list(headers),
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
    rows = list((await session.scalars(select(Setting).where(Setting.key.in_(PROVIDER_KEYS)))).all())
    if not rows:
        return
    provider_id = "provider-" + uuid.uuid5(uuid.NAMESPACE_URL, "vireyabot/provider/legacy-default").hex[:24]
    if await session.scalar(select(Provider).where(Provider.provider_id == provider_id)) is not None:
        for row in rows:
            await session.delete(row)
        await session.commit()
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
        "protocol_type": provider.protocol_type,
        "base_url": provider.base_url,
        "api_key": decrypt(secret.api_key) if secret.api_key else "",
        "responses_model": model.responses_model,
        "embedding_model": model.embedding_model,
        "embedding_dimensions": model.embedding_dimensions,
        "timeout_seconds": provider.timeout_seconds,
        "project": provider.project,
        "organization": provider.organization,
        "max_output_tokens": model.max_output_tokens,
        "native_search_enabled": model.native_search_enabled,
        "default_stream": model.default_stream,
        "temperature": model.temperature,
        "extra_headers": _read_provider_headers(secret),
    }


def _read_provider_headers(secret: ProviderSecret | None) -> dict[str, str]:
    # 0003 added an empty, unencrypted server default to existing rows.
    return json.loads(decrypt(secret.extra_headers)) if secret and secret.extra_headers else {}


def _provider_headers(rows: object, existing: dict[str, str]) -> dict[str, str]:
    if not isinstance(rows, list) or len(rows) > 32:
        raise ValueError("extra_headers must contain at most 32 rows")
    original = {key.lower(): value for key, value in existing.items()}
    names: set[str] = set()
    sources: set[str] = set()
    result: dict[str, str] = {}
    for index, row in enumerate(rows):
        try:
            header = HeaderRow.model_validate(row)
        except ValueError:
            raise ValueError(f"extra_headers.{index}: invalid or reserved header") from None
        normalized = header.name.lower()
        previous = (header.previous_name or header.name).lower()
        if normalized in names or previous in sources:
            raise ValueError(f"extra_headers.{index}.name: duplicate header")
        if header.previous_name is not None and previous not in original:
            raise ValueError(f"extra_headers.{index}.previous_name: unknown header")
        value = header.value or original.get(previous)
        if value is None or value == "":
            raise ValueError(f"extra_headers.{index}.value: new header requires a value")
        names.add(normalized)
        sources.add(previous)
        result[header.name] = value
    return result


def _validate_provider_fields(values: Mapping[str, object], protocol: str) -> None:
    if protocol not in PROVIDER_PROTOCOLS:
        raise ValueError("protocol_type is unsupported")
    allowed = set(PROVIDER_PROTOCOLS[protocol]["fields"]) | {"confirm_protocol_change"}
    if any(key not in allowed for key in values):
        raise ValueError("protocol_type: request contains an unknown or inapplicable field")
    nullable = {"temperature", "embedding_dimensions", "api_key"}
    for key, value in values.items():
        if value is None and key not in nullable:
            raise ValueError(f"{key}: null is not permitted")


def _new_provider_id() -> str:
    return "provider-" + uuid.uuid4().hex[:24]


async def create_provider_config(session: AsyncSession, values: Mapping[str, object], username: str) -> dict[str, object]:
    protocol = values.get("protocol_type")
    _validate_provider_fields(values, protocol)
    _validate_provider_secret(values.get("api_key"))
    headers = _provider_headers(values.get("extra_headers", []), {})
    runtime = {"default_stream": values.get("default_stream", False), "temperature": values.get("temperature"), "max_output_tokens": values.get("max_output_tokens", 4096)}
    _validate_provider_runtime_options(runtime)
    candidate = {"name": values.get("name"), "protocol_type": protocol, "base_url": values.get("base_url"), "timeout_seconds": values.get("timeout_seconds"), "organization": values.get("organization", ""), "project": values.get("project", ""), "enabled": values.get("enabled", True), "responses_model": values.get("responses_model"), "embedding_model": values.get("embedding_model", ""), "embedding_dimensions": values.get("embedding_dimensions"), "native_search_enabled": values.get("native_search_enabled", False), **runtime}
    _validate_provider_values(candidate)
    provider = Provider(provider_id=_new_provider_id(), **{key: candidate[key] for key in ("name", "protocol_type", "base_url", "timeout_seconds", "organization", "project", "enabled")})
    session.add(provider)
    await session.flush()
    session.add(ProviderSecret(provider_id=provider.id, api_key=encrypt(str(values.get("api_key") or "")), extra_headers=encrypt(json.dumps(headers))))
    model = ProviderModelConfig(provider_id=provider.id, responses_model=candidate["responses_model"], embedding_model=candidate["embedding_model"], embedding_dimensions=candidate["embedding_dimensions"], native_search_enabled=candidate["native_search_enabled"], default_stream=runtime["default_stream"], temperature=runtime["temperature"], max_output_tokens=runtime["max_output_tokens"])
    session.add(model)
    session.add(ConfigChange(key=f"provider:{provider.provider_id}", admin_username=username, action="create"))
    await session.commit()
    return _provider_public(provider, await session.scalar(select(ProviderSecret).where(ProviderSecret.provider_id == provider.id)), model)


async def update_provider_config(session: AsyncSession, provider_id: str, values: Mapping[str, object], username: str) -> dict[str, object]:
    _validate_provider_secret(values.get("api_key"))
    provider = await session.scalar(select(Provider).where(Provider.provider_id == provider_id))
    if provider is None:
        raise ValueError("Unknown providerId")
    secret = await session.scalar(select(ProviderSecret).where(ProviderSecret.provider_id == provider.id))
    model = await session.scalar(select(ProviderModelConfig).where(ProviderModelConfig.provider_id == provider.id))
    if secret is None or model is None:
        raise ValueError("Provider configuration is incomplete")
    protocol = values.get("protocol_type", provider.protocol_type)
    _validate_provider_fields(values, protocol)
    changing_protocol = protocol != provider.protocol_type
    if changing_protocol and values.get("confirm_protocol_change") is not True:
        raise ValueError("confirm_protocol_change: confirm removal of protocol-specific fields")
    headers = _read_provider_headers(secret)
    if "extra_headers" in values:
        headers = _provider_headers(values["extra_headers"], headers)
    runtime = {"default_stream": values.get("default_stream", model.default_stream), "temperature": values.get("temperature", None if changing_protocol else model.temperature), "max_output_tokens": values.get("max_output_tokens", model.max_output_tokens)}
    _validate_provider_runtime_options(runtime)
    candidate = {"name": values.get("name", provider.name), "protocol_type": protocol, "base_url": values.get("base_url", provider.base_url), "timeout_seconds": values.get("timeout_seconds", provider.timeout_seconds), "organization": values.get("organization", provider.organization), "project": values.get("project", provider.project), "enabled": values.get("enabled", provider.enabled), "responses_model": values.get("responses_model", model.responses_model), "embedding_model": values.get("embedding_model", model.embedding_model), "embedding_dimensions": values.get("embedding_dimensions", model.embedding_dimensions), "native_search_enabled": values.get("native_search_enabled", model.native_search_enabled), **runtime}
    if protocol == "anthropic_messages":
        candidate.update(organization="", project="", embedding_model="", embedding_dimensions=None)
    if protocol != "openai_responses":
        candidate["native_search_enabled"] = False
    _validate_provider_values(candidate)
    provider.name, provider.protocol_type, provider.base_url = candidate["name"], candidate["protocol_type"], candidate["base_url"]
    provider.timeout_seconds, provider.organization, provider.enabled = candidate["timeout_seconds"], candidate["organization"], candidate["enabled"]
    provider.project = candidate["project"]
    model.responses_model, model.embedding_model = candidate["responses_model"], candidate["embedding_model"]
    model.embedding_dimensions, model.native_search_enabled = candidate["embedding_dimensions"], candidate["native_search_enabled"]
    model.default_stream, model.temperature, model.max_output_tokens = runtime["default_stream"], runtime["temperature"], runtime["max_output_tokens"]
    if values.get("api_key") not in {None, "", MASKED_SECRET}:
        secret.api_key = encrypt(str(values["api_key"]))
    secret.extra_headers = encrypt(json.dumps(headers))
    session.add(ConfigChange(key=f"provider:{provider_id}", admin_username=username, action="update"))
    await session.commit()
    return _provider_public(provider, secret, model)


async def delete_provider_config(session: AsyncSession, provider_id: str, username: str) -> None:
    provider = await session.scalar(select(Provider).where(Provider.provider_id == provider_id))
    if provider is None:
        raise ValueError("Unknown providerId")
    await session.delete(provider)
    session.add(ConfigChange(key=f"provider:{provider_id}", admin_username=username, action="delete"))
    await session.commit()





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
