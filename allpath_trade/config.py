from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

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


def normalize_llm_provider(provider: str) -> str:
    """The single canonical reading of `LLM_PROVIDER`.

    `llm/factory.py` decides which client to build from it, and
    `web/setup_status.py` decides whether that provider's key is missing --
    if those two normalized the same string differently, the setup gate
    could call an install configured while the factory refused to build
    anything for it (or the reverse: a gate that never lets go). One
    function so they cannot drift.

    Case-folded only, deliberately not stripped: an `LLM_PROVIDER` with
    stray whitespace is a typo in `.env`, and reporting it as unknown --
    from both call sites, identically -- is more useful than silently
    accepting it in one place and not the other.
    """
    return provider.lower()


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
        #
        # M4: the scheme check is case-insensitive (a pasted "HTTPS://..."
        # is a perfectly valid URL that a case-sensitive `.startswith`
        # rejected) and requires a host beyond the scheme -- `urlsplit`
        # rather than `.startswith`, since "http://" alone previously
        # passed the old check and would have built a link to nowhere
        # (f"{base}/a/{id}?k=..." -> "http:///a/1?k=...").
        if not v:
            return v
        stripped = v.rstrip("/")
        parsed = urlsplit(stripped)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "must be empty or a URL with http:// or https:// and a host")
        return stripped
    daily_consolidation: bool = True
    consolidate_after_chat: bool = True
    # Phase 6: the after-close reflection session's own tool-call budget,
    # separate from a live chat's max_iters -- a reflection session runs
    # unattended (no user to say "keep going" past LIMIT_NOTICE), so it gets
    # its own cap rather than sharing whatever a chat session happens to use.
    reflection_max_iters: int = Field(default=12, ge=1)
    # I8: the reflection pass's WALL-CLOCK bound, the companion to the
    # tool-call budget above. `reflection_max_iters` caps how many provider
    # round-trips a pass may make, but says nothing about how long each one
    # takes -- 12 iterations that each sit near `llm_timeout_seconds`, plus
    # slow tool calls between them, is most of an hour for ONE account, and
    # the nightly chain runs every account in sequence. Checked between
    # iterations (reflect.py's `_DeadlineGuard`): once the budget is spent
    # the pass stops researching and is forced straight into the same
    # one-iteration wrap-up turn the iteration cap already triggers, so the
    # day still gets a real report rather than a `failed` row. 0 disables
    # the bound entirely (the pre-I8 behaviour). Not on the settings page,
    # same .env-only policy as REFLECTION_MAX_ITERS above.
    reflection_deadline_seconds: int = Field(default=1800, ge=0)
    # Gates the after-close reflection job the same way daily_consolidation
    # gates the consolidator -- not on the settings page yet, .env-only.
    daily_reflection: bool = True
    # Ops-hardening: both LLM SDK clients (anthropic_client.py,
    # openai_compat.py) are constructed with no explicit timeout, so a
    # genuinely hung HTTP call (not erroring -- hanging) falls back to each
    # SDK's own default (anthropic ~10min with retries; openai similar).
    # That's long enough to block the entire after-close chain (digest ->
    # reflection -> consolidation, all synchronous in one job) and every
    # sentinel tick behind it, since APScheduler's max_instances=1 means a
    # stuck job silently swallows every tick queued behind it -- the
    # heartbeat going stale becomes the only symptom. 180s is generous for
    # reflection's memory-tier calls (Opus, large prompts) while still being
    # finite; not on the settings page yet, same .env-only policy as
    # REFLECTION_MAX_ITERS. Floor of 10s guards against a value so low every
    # call would time out immediately.
    llm_timeout_seconds: int = Field(default=180, ge=10)
    # Two-week-autonomous-run experiment (spec 2026-08-26): reflection's own
    # revision proposals auto-apply through the normal applier when this is
    # on. .env-only by design -- flipping it is an experiment decision, not
    # a settings-page toggle a user should reach for casually.
    experiment_auto_apply_revisions: bool = False
    # Drawdown circuit breaker: halt auto trading when equity falls this
    # fraction below its recorded peak. 0 disables the breaker entirely.
    drawdown_halt_pct: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1)
    # Alpaca's TradingClient issues requests with NO socket timeout (see
    # docs/TODO.md's _broker_pool note) -- one hung call stalls a sentinel
    # tick silently. yfinance already defaults to 30s internally.
    broker_http_timeout_seconds: int = Field(default=30, ge=5)
    # Telegram chat channel (Task 1 of the Telegram plan): the bot token from
    # BotFather (@BotFather -> /newbot). Empty (the default) means the
    # channel is off -- no poller starts, no pairing is possible.
    telegram_bot_token: str = ""


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
