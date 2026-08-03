from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values, set_key
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Compactor reserves at least MIN_SUMMARY_RESERVE_TOKENS (600) off the cut
# target for the summary it's about to write, unconditionally (see
# agent/compact.py) -- a budget at or below that leaves no room for an actual
# conversation turn, so every single turn would force a summarization call.
# 2000 is comfortably above that floor without needing to know Compactor's
# internals to justify the number.
MIN_CONTEXT_BUDGET_TOKENS = 2000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_paper: bool = True
    db_path: Path = Path("allpath-trade.db")
    smtp_host: str = ""
    # 1-65535: the valid TCP port range -- 0 and negative values aren't a
    # port at all, and would surface as an opaque connection failure only
    # once email notifications are actually attempted.
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_to: str = ""
    # Same class of brick Task 12 set out to prevent (see Finding 4 of the
    # Phase 5 final review): APScheduler's IntervalTrigger does not validate
    # positivity. 0 or a negative interval doesn't fail to schedule -- it
    # schedules a job that is perpetually overdue, so the sentinel pass runs
    # back-to-back in a hot loop against the broker, yfinance, and the
    # review-tier LLM. The type-only check the settings route already ran
    # (Settings(**candidate) before writing to disk) let a negative int
    # straight through; the constraint has to live on the field itself so
    # both the settings page and process startup are covered from one place.
    sentinel_interval_minutes: int = Field(default=60, ge=1)
    strategies_dir: Path = Path("strategies")
    llm_provider: str = "openrouter"
    openrouter_api_key: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    chat_model: str = "anthropic/claude-sonnet-5"
    review_model: str = "anthropic/claude-haiku-4.5"
    # Consolidation decides what enters long-term memory; a bad call there
    # pollutes every later conversation, so it gets the strongest tier.
    memory_model: str = "anthropic/claude-opus-5"
    memory_dir: Path = Path("memory")
    context_budget_tokens: int = Field(default=60000, ge=MIN_CONTEXT_BUDGET_TOKENS)
    web_host: str = "127.0.0.1"
    web_port: int = 8791
    web_token: str = ""
    daily_consolidation: bool = True
    consolidate_after_chat: bool = True


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
        # quote_mode="always": values reach here from the settings page and may
        # contain spaces, '#', or '=' — unquoted, dotenv truncates or mangles them.
        self.env_file.touch(exist_ok=True)
        set_key(str(self.env_file), key, value, quote_mode="always")

    def load(self) -> Settings:
        return Settings(_env_file=self.env_file)
