"""LLM outage visibility (ops 2026-11-06): a week of 402 `openrouter_credits`
failures went unnoticed. Every failed call through the recording choke
point is now recorded, classified, and -- for the account-level classes
(credits / auth / rate limit) -- alerted to the operator, throttled."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from allpath_trade.llm.base import LLMError, LLMResponse
from allpath_trade.llm.factory import _RecordingClient
from allpath_trade.llm.health import (
    LLMOutageAlerter,
    classify_llm_error,
    llm_health_line,
)
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY, AppState
from allpath_trade.store.db import connect
from allpath_trade.store.llm_usage import LLMUsage


class StatusError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def wrapped(message: str, status: int | None = None) -> LLMError:
    """An LLMError the way both clients raise one: `raise wrap(...) from exc`."""
    cause = StatusError(message, status) if status is not None else Exception(message)
    try:
        raise LLMError(f"llm request failed: {message}") from cause
    except LLMError as err:
        return err


# -- classification -------------------------------------------------------

@pytest.mark.parametrize("exc,expected", [
    (wrapped("Error code: 402 - {'error': {'message': 'Insufficient credits', "
             "'code': 402, 'metadata': {'reason': 'openrouter_credits'}}}", 402),
     "credits"),
    # No status on the cause: the openai SDK's message text still says it.
    (wrapped("Error code: 402 - insufficient credits"), "credits"),
    (wrapped("openrouter_credits: add more credits"), "credits"),
    (wrapped("Error code: 401 - No auth credentials found", 401), "auth"),
    (wrapped("Error code: 403 - key disabled", 403), "auth"),
    (wrapped("Error code: 429 - Key limit exceeded (daily limit)", 429), "rate_limit"),
    (wrapped("Error code: 429 - rate limited"), "rate_limit"),
    (wrapped("Error code: 500 - upstream exploded", 500), "other"),
    (wrapped("Request timed out."), "other"),
])
def test_classify_llm_error(exc, expected):
    assert classify_llm_error(exc) == expected


# -- the recording choke point records failures ----------------------------

class FailingInner:
    model = "openai/gpt-6.1-sol"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, messages, tools=None):
        raise self.exc


class OkInner:
    model = "anthropic/claude-haiku-4.5"

    def complete(self, messages, tools=None):
        return LLMResponse(text="ok", input_tokens=10, output_tokens=2)


def usage_store(tmp_path) -> LLMUsage:
    return LLMUsage(connect(tmp_path / "t.db"))


def test_recording_client_records_a_failed_call_and_reraises(tmp_path):
    usage = usage_store(tmp_path)
    client = _RecordingClient(FailingInner(wrapped("Error code: 402 - credits", 402)),
                              "memory", usage)
    with pytest.raises(LLMError):
        client.complete([{"role": "user", "content": "hi"}])
    failures = usage.failures_for_day()
    assert failures == {"memory": {"count": 1, "classes": ["credits"]}}


class SpyAlerter:
    def __init__(self, explode: bool = False) -> None:
        self.calls: list[tuple] = []
        self.explode = explode

    def on_failure(self, error_class, tier, model, detail):
        self.calls.append((error_class, tier, model))
        if self.explode:
            raise RuntimeError("alerter broke")


def test_recording_client_hands_failures_to_the_store_alerter(tmp_path):
    usage = usage_store(tmp_path)
    usage.alerter = SpyAlerter()
    client = _RecordingClient(FailingInner(wrapped("Error code: 429 - limit", 429)),
                              "review", usage)
    with pytest.raises(LLMError):
        client.complete([])
    assert usage.alerter.calls == [("rate_limit", "review", "openai/gpt-6.1-sol")]


def test_a_broken_alerter_never_masks_the_original_llm_error(tmp_path):
    usage = usage_store(tmp_path)
    usage.alerter = SpyAlerter(explode=True)
    client = _RecordingClient(FailingInner(wrapped("Error code: 402", 402)), "chat", usage)
    with pytest.raises(LLMError, match="402"):
        client.complete([])


def test_last_success_by_tier_reads_the_newest_usage_row(tmp_path):
    usage = usage_store(tmp_path)
    _RecordingClient(OkInner(), "review", usage).complete([])
    last = usage.last_success_by_tier()
    assert set(last) == {"review"}
    assert last["review"].startswith(datetime.now(UTC).date().isoformat())


# -- the throttled alert ----------------------------------------------------

class SpyNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send(self, subject, body):
        self.sent.append((subject, body))
        return True


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def alerter(tmp_path, **over):
    conn = connect(tmp_path / "a.db")
    notifier = SpyNotifier()
    clock = Clock()
    kwargs = {"app_state": AppState(conn), "notifier": notifier,
              "telegram_bot_token": "", "clock": clock}
    kwargs.update(over)
    return LLMOutageAlerter(**kwargs), notifier, clock


def test_credit_failure_sends_an_alert_with_the_fix(tmp_path):
    a, notifier, _clock = alerter(tmp_path)
    a.on_failure("credits", "review", "anthropic/claude-haiku-4.5",
                 "llm request failed: Error code: 402 - openrouter_credits")
    [(subject, body)] = notifier.sent
    assert "LLM" in subject and "credits" in subject.lower()
    assert "top up" in body.lower()
    assert "review" in body and "anthropic/claude-haiku-4.5" in body
    assert "402" in body


def test_rate_limit_alert_says_to_raise_the_key_limit(tmp_path):
    a, notifier, _clock = alerter(tmp_path)
    a.on_failure("rate_limit", "memory", "m", "Error code: 429 - Key limit exceeded")
    [(_subject, body)] = notifier.sent
    assert "key limit" in body.lower()


def test_auth_alert_says_to_check_the_key(tmp_path):
    a, notifier, _clock = alerter(tmp_path)
    a.on_failure("auth", "chat", "m", "Error code: 401")
    [(_subject, body)] = notifier.sent
    assert "API key" in body


def test_other_failures_are_not_alerted(tmp_path):
    a, notifier, _clock = alerter(tmp_path)
    a.on_failure("other", "chat", "m", "Request timed out.")
    assert notifier.sent == []


def test_alerts_are_throttled_per_error_class(tmp_path):
    a, notifier, clock = alerter(tmp_path)
    a.on_failure("credits", "review", "m", "402")
    a.on_failure("credits", "memory", "m", "402")       # same class, same window
    a.on_failure("rate_limit", "memory", "m", "429")    # different class: alerts
    assert len(notifier.sent) == 2
    clock.now += timedelta(hours=2, minutes=59)
    a.on_failure("credits", "chat", "m", "402")
    assert len(notifier.sent) == 2
    clock.now += timedelta(minutes=2)                   # window passed
    a.on_failure("credits", "chat", "m", "402")
    assert len(notifier.sent) == 3


def test_alert_also_reaches_the_paired_telegram_chat(tmp_path, monkeypatch):
    sent: list[tuple[str, str]] = []

    class FakeAPI:
        def __init__(self, token):
            self.token = token

        def send_message(self, chat_id, text, reply_markup=None):
            sent.append((chat_id, text))

    from allpath_trade.notify import dispatch
    monkeypatch.setattr(dispatch, "TelegramAPI", FakeAPI)
    a, _notifier, _clock = alerter(tmp_path, telegram_bot_token="tok")
    a.app_state.set(TELEGRAM_CHAT_ID_KEY, "42")
    a.on_failure("credits", "review", "m", "402")
    [(chat_id, text)] = sent
    assert chat_id == "42" and "top up" in text.lower()


def test_a_failing_notifier_never_raises(tmp_path):
    class Boom:
        def send(self, subject, body):
            raise RuntimeError("smtp down")

    a, _n, _c = alerter(tmp_path, notifier=Boom())
    a.on_failure("credits", "review", "m", "402")  # must not raise


# -- the digest's health line -------------------------------------------------

def test_health_line_is_empty_with_no_llm_history(tmp_path):
    assert llm_health_line(usage_store(tmp_path)) == ""


def test_health_line_names_last_success_and_todays_failures_per_tier(tmp_path):
    usage = usage_store(tmp_path)
    old = (datetime.now(UTC) - timedelta(days=7)).replace(microsecond=0)
    usage._conn.execute(
        "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens, purpose)"
        " VALUES (?, 'review', 'm', 1, 1, 'review')", (old.isoformat(),))
    usage._conn.commit()
    _RecordingClient(OkInner(), "chat", usage).complete([])
    for _ in range(3):
        usage.record_failure(tier="review", model="m", error_class="credits",
                             detail="402")

    line = llm_health_line(usage)
    assert line.startswith("⚠")
    assert "review" in line and old.strftime("%m-%d") in line
    assert "3 failed today (credits)" in line
    assert "chat" in line and "0 failed today" in line
    assert "memory" in line and "never" in line


def test_health_line_has_no_warning_mark_when_nothing_failed(tmp_path):
    usage = usage_store(tmp_path)
    _RecordingClient(OkInner(), "chat", usage).complete([])
    assert not llm_health_line(usage).startswith("⚠")
