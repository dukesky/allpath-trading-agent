from __future__ import annotations

from pathlib import Path

from dotenv import dotenv_values, set_key
from pydantic import Field, ValidationError, field_validator
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
    # Push notification channel via ntfy (https://ntfy.sh or self-hosted):
    # the full topic URL the app POSTs to, e.g. "https://ntfy.sh/my-topic".
    # Blank means the channel is not configured -- see build_notifier.
    ntfy_url: str = ""

    @field_validator("ntfy_url")
    @classmethod
    def _ntfy_url_needs_a_scheme(cls, v: str) -> str:
        # urllib.request.Request raises ValueError("unknown url type") for a
        # scheme-less URL -- and the settings-page hint literally says "paste
        # the topic URL here", so a value like "ntfy.sh/my-topic" (no
        # "https://") is the expected mistake, not an edge case. Catching it
        # here means the settings page rejects it with an inline error before
        # it ever reaches .env, instead of persisting a value that later
        # bricks the sentinel's send (undercounting the daily digest) and the
        # save-and-test POST (a 500 after the bad value was already saved).
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must be empty or start with http:// or https://")
        return v

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
    # Approve-by-link (Part A): opt-in. Empty (the default) means
    # notifications carry no links at all -- the sentinel only builds a
    # `/a/<id>?k=<token>` URL when this is set, since a link is only useful
    # if it points somewhere the user's phone can actually reach (not
    # 127.0.0.1, which is what web_host defaults to and is meaningless off
    # the machine the server runs on).
    web_base_url: str = ""

    @field_validator("web_base_url")
    @classmethod
    def _web_base_url_needs_a_scheme(cls, v: str) -> str:
        # Same shape as `_ntfy_url_needs_a_scheme` below: fail loudly on the
        # settings page (before writing to .env) rather than silently
        # building a broken link later. Trailing slash stripped once here so
        # every URL-builder downstream can just do f"{base}/a/{id}?k=..."
        # without checking for a double slash.
        if v and not v.startswith(("http://", "https://")):
            raise ValueError("must be empty or start with http:// or https://")
        return v.rstrip("/")
    daily_consolidation: bool = True
    consolidate_after_chat: bool = True
    # Phase 6: the after-close reflection session's own tool-call budget,
    # separate from a live chat's max_iters -- a reflection session runs
    # unattended (no user to say "keep going" past LIMIT_NOTICE), so it gets
    # its own cap rather than sharing whatever a chat session happens to use.
    reflection_max_iters: int = Field(default=12, ge=1)
    # Gates the after-close reflection job the same way daily_consolidation
    # gates the consolidator -- not on the settings page yet, .env-only.
    daily_reflection: bool = True


def describe_validation_error(exc: ValidationError) -> str:
    """One readable line per failing field, e.g. "sentinel_interval_minutes:
    Input should be greater than or equal to 1" -- shared by the settings
    route (pre-write validation, F1's sibling) and SettingsStore.load's own
    caller in cli.py (an already-on-disk value that was legal before a range
    constraint landed on it, per Finding 4)."""
    parts = [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]
    return "; ".join(parts)


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
