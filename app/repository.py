import json
from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Admin, ConfigChange, Setting
from app.security import encrypt, hash_password, verify_password

SECRET_KEYS = {"integration.test_secret"}
DEFAULT_VALUES = {
    "app.name": ("VireyaBot", False),
    "logging.level": ("INFO", False),
    "features.proactive_enabled": (False, False),
    "integration.test_secret": ("", True),
}


def _flatten(data: Mapping, prefix: str = "") -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            result.update(_flatten(value, path))
        elif path in {"app.name", "logging.level", "features.proactive_enabled"}:
            result[path] = value
    return result


async def bootstrap(session: AsyncSession) -> None:
    admin = await session.scalar(select(Admin).where(Admin.username == settings.admin_username))
    if admin is None:
        session.add(Admin(username=settings.admin_username, password_hash=hash_password(settings.admin_password)))
    defaults = {**DEFAULT_VALUES, **{key: (value, False) for key, value in _flatten(settings.defaults).items()}}
    for key, (value, is_secret) in defaults.items():
        existing = await session.scalar(select(Setting).where(Setting.key == key))
        if existing is None:
            stored = encrypt(str(value)) if is_secret else json.dumps(value)
            session.add(Setting(key=key, value=stored, is_secret=is_secret))
    await session.commit()


async def authenticate(session: AsyncSession, username: str, password: str) -> bool:
    admin = await session.scalar(select(Admin).where(Admin.username == username, Admin.is_active.is_(True)))
    return bool(admin and verify_password(password, admin.password_hash))


async def read_settings(session: AsyncSession) -> dict[str, object]:
    rows = (await session.scalars(select(Setting).order_by(Setting.key))).all()
    result = {}
    for row in rows:
        result[row.key] = "••••••••" if row.is_secret else json.loads(row.value)
    return result


async def save_settings(session: AsyncSession, values: Mapping[str, object], username: str) -> None:
    for key, value in values.items():
        row = await session.scalar(select(Setting).where(Setting.key == key))
        if row is None or key not in DEFAULT_VALUES:
            raise ValueError(f"Unknown setting: {key}")
        if row.is_secret and (value is None or value == "••••••••" or value == ""):
            continue
        if key == "features.proactive_enabled" and not isinstance(value, bool):
            raise ValueError("features.proactive_enabled must be boolean")
        if key != "features.proactive_enabled" and not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        row.value = encrypt(value) if row.is_secret else json.dumps(value)
        session.add(ConfigChange(key=key, admin_username=username, action="update"))
    await session.commit()
