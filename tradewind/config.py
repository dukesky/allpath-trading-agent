from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values, set_key
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    db_path: Path = Path("tradewind.db")


class SettingsStore:
    """Single read/write path for local config. Backed by a .env file.

    Real environment variables still override file values when loading
    Settings (pydantic-settings behavior)."""

    def __init__(self, env_file: Path = Path(".env")) -> None:
        self.env_file = env_file

    def get(self, key: str) -> str | None:
        if not self.env_file.exists():
            return None
        return dotenv_values(self.env_file).get(key)

    def set(self, key: str, value: str) -> None:
        self.env_file.touch(exist_ok=True)
        set_key(str(self.env_file), key, value, quote_mode="never")

    def load(self) -> Settings:
        return Settings(_env_file=self.env_file)
