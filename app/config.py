from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://vireyabot:vireyabot@localhost:5432/vireyabot"
    config_master_key: str
    admin_username: str = "admin"
    admin_password: str
    default_config_path: str = "config/defaults.yaml"
    vireyabot_version: str = "0.1.0"
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def defaults(self) -> dict:
        path = Path(self.default_config_path)
        return yaml.safe_load(path.read_text()) if path.exists() else {}


settings = Settings()
