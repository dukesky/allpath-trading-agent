"""LLM outage visibility (ops 2026-11-06).

OpenRouter credits ran out on 2026-11-02 and every LLM call failed with 402
`openrouter_credits` for five days. Nothing told anyone: reflection, the
order ReviewAgent, chat and memory consolidation were all down, and order
reviews reached Telegram with an empty agent analysis that looked normal.

Three pieces live here:

- `classify_llm_error`: maps a failed call to credits | auth | rate_limit |
  other. The first three are account-level problems that only a human can
  fix (top up, replace the key, raise the key's limit).
- `LLMOutageAlerter`: sends an immediate alert (notifier + paired Telegram
  chat) for those three classes, at most once per class per throttle window.
  `llm/factory.py`'s `_RecordingClient` hands it every failed call.
- `llm_health_line`: the daily digest's per-tier "last OK / failed today"
  line, read from `llm_usage` (successes) and `llm_failures` (failures).
"""

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

TIERS = ("chat", "memory", "review")

ALERT_THROTTLE = timedelta(hours=3)

_ALERT_KEY = "llm_alert_last:{}"

_STATUS_IN_TEXT = re.compile(r"\b(?:error code|status)[:\s]+(\d{3})\b", re.IGNORECASE)
_CREDITS_TEXT = re.compile(r"openrouter_credits|insufficient credits|"
                           r"out of credits|credit balance|more credits",
                           re.IGNORECASE)
_LIMIT_TEXT = re.compile(r"key limit|daily limit|spending limit|rate limit",
                         re.IGNORECASE)

# error class -> (what happened, how to fix it). Shown verbatim in the alert.
_CLASSES: dict[str, tuple[str, str]] = {
    "credits": ("LLM provider is out of credits (HTTP 402)",
                ("Top up credits at https://openrouter.ai/settings/credits, "
                 "then check the key's limit.")),
    "rate_limit": ("LLM key hit its rate or spending limit (HTTP 429)",
                   ("Raise the key limit at https://openrouter.ai/settings/keys "
                    "(or wait for the daily reset), and check the credit balance.")),
    "auth": ("LLM API key was rejected (HTTP 401/403)",
             "Check or replace the API key (OPENROUTER_API_KEY) in Settings."),
}


def _status_of(exc: BaseException) -> int | None:
    for candidate in (exc, exc.__cause__):
        status = getattr(candidate, "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool):
            return status
    match = _STATUS_IN_TEXT.search(str(exc))
    return int(match.group(1)) if match else None


def classify_llm_error(exc: BaseException) -> str:
    """credits | auth | rate_limit | other. Reads the provider status from
    the wrapped SDK exception (`LLMError.__cause__`), falling back to the
    "Error code: NNN" the openai SDK puts in its message, then to the text."""
    status = _status_of(exc)
    text = f"{exc} {exc.__cause__ or ''}"
    if status == 402 or _CREDITS_TEXT.search(text):
        return "credits"
    if status in (401, 403):
        return "auth"
    if status == 429 or _LIMIT_TEXT.search(text):
        return "rate_limit"
    return "other"


class LLMOutageAlerter:
    """Alerts the operator when LLM calls fail for an account-level reason.

    `on_failure` never raises. The throttle stamp (`llm_alert_last:{class}`
    in app_state) is written before sending, so a dead channel cannot turn
    into an alert storm; the next alert for that class comes after
    `throttle` at the earliest."""

    def __init__(self, *, app_state, notifier, telegram_bot_token: str = "",
                 throttle: timedelta = ALERT_THROTTLE,
                 clock: Callable[[], datetime] | None = None) -> None:
        self.app_state = app_state
        self.notifier = notifier
        self.telegram_bot_token = telegram_bot_token
        self.throttle = throttle
        self._clock = clock or (lambda: datetime.now(UTC))

    def on_failure(self, error_class: str, tier: str, model: str, detail: str) -> None:
        if error_class not in _CLASSES:
            return
        try:
            if not self._due(error_class):
                return
            subject, body = self._message(error_class, tier, model, detail)
        except Exception as exc:  # noqa: BLE001 — alerting must never break a call
            print(f"[llm-health] alert skipped: {exc}", file=sys.stderr)
            return
        if self.notifier is not None:
            try:
                self.notifier.send(subject, body)
            except Exception as exc:  # noqa: BLE001 — Notifier.send should not raise
                print(f"[llm-health] notifier failed: {exc}", file=sys.stderr)
        from allpath_trade.notify.dispatch import push_telegram_alert

        push_telegram_alert(app_state=self.app_state,
                            telegram_bot_token=self.telegram_bot_token, body=body)

    def _due(self, error_class: str) -> bool:
        now = self._clock()
        key = _ALERT_KEY.format(error_class)
        last = self.app_state.get(key) if self.app_state is not None else None
        if last:
            try:
                if now - datetime.fromisoformat(last) < self.throttle:
                    return False
            except ValueError:
                pass  # a corrupt stamp must not silence alerts forever
        if self.app_state is not None:
            self.app_state.set(key, now.isoformat())
        return True

    def _message(self, error_class: str, tier: str, model: str,
                 detail: str) -> tuple[str, str]:
        from allpath_trade.notify.events import Subject

        what, fix = _CLASSES[error_class]
        hours = self.throttle.total_seconds() / 3600
        subject = Subject(f"[AllPath] LLM DOWN: {what}",
                          {"type": "llm_outage", "error_class": error_class,
                           "tier": tier, "model": model})
        body = (f"⚠ {what}.\n"
                f"Failed call: {tier} tier, model {model}.\n"
                f"Fix: {fix}\n"
                "Until then reflection, order reviews (no agent analysis), "
                "chat and memory consolidation are not working. Rule-based "
                "trading continues.\n"
                f"Error: {detail[:300]}\n"
                f"(Next alert for this error class in {hours:g}h at the earliest.)")
        return subject, body


def _short_ts(ts: str) -> str:
    return f"{ts[5:10]} {ts[11:16]}Z"


def llm_health_line(usage, date_utc: str | None = None) -> str:
    """One digest line: per tier, the last successful call and today's
    failures. `""` when there is no LLM history at all (nothing configured
    or never used), so such a digest stays as before. Starts with "⚠" when
    any call failed today."""
    last_ok = usage.last_success_by_tier()
    failures = usage.failures_for_day(date_utc)
    if not last_ok and not failures:
        return ""
    parts = []
    for tier in sorted(set(TIERS) | set(last_ok) | set(failures)):
        ok = (f"last OK {_short_ts(last_ok[tier])}" if tier in last_ok
              else "never succeeded")
        failed = failures.get(tier)
        fail_text = (f"{failed['count']} failed today ({', '.join(failed['classes'])})"
                     if failed else "0 failed today")
        parts.append(f"{tier} {ok}, {fail_text}")
    mark = "⚠ " if failures else ""
    return f"{mark}LLM health: " + "; ".join(parts) + "."
