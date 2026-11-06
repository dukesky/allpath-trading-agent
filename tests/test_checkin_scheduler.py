"""Shadow check-in pilot: the nightly-chain step (spec
2026-11-06-shadow-checkin-design.md)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import ClassVar

from allpath_trade.scheduler import run_daily_jobs
from allpath_trade.store.app_state import TELEGRAM_CHAT_ID_KEY
from tests.test_scheduler import (
    FakeAppState,
    FakeReflector,
    FakeSentinel,
    FakeStrategies,
    _account_bundle,
    _components,
)


class SpyCheckin:
    instances: ClassVar[list] = []
    fail = False

    def __init__(self, *, llm, acc, api, chat_id, account="shadow"):
        self.acc, self.chat_id = acc, chat_id
        SpyCheckin.instances.append(self)

    def run_daily(self, now=None):
        if SpyCheckin.fail:
            raise RuntimeError("402 out of credits")
        self.acc.order.append("checkin")
        return "sent 2 question(s)"


class OrderedReflector(FakeReflector):
    def __init__(self, order):
        super().__init__()
        self.order = order

    def run_daily(self, now=None):
        self.order.append("reflection")
        return super().run_daily()


def _setup(monkeypatch, *, enabled=True, paired=True, token="bot-token"):
    SpyCheckin.instances, SpyCheckin.fail = [], False
    monkeypatch.setattr("allpath_trade.checkin.DailyCheckin", SpyCheckin)
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat", usage_store=None: f"llm:{tier}")
    order: list[str] = []
    shadow = _account_bundle(strategies=FakeStrategies(active=True),
                             reflector=OrderedReflector(order))
    shadow.order = order
    app_state = FakeAppState()
    if paired:
        app_state.set(TELEGRAM_CHAT_ID_KEY, "111")
    components = _components(sentinel=FakeSentinel(), app_state=app_state,
                             accounts={"paper": _account_bundle(), "shadow": shadow})
    components.settings = SimpleNamespace(
        **vars(components.settings), shadow_daily_checkin=enabled,
        telegram_bot_token=token)
    return components, order


def test_flag_off_never_builds_a_checkin(monkeypatch):
    components, _ = _setup(monkeypatch, enabled=False)
    assert run_daily_jobs(components) is True
    assert SpyCheckin.instances == []


def test_runs_after_shadow_reflection_with_the_paired_chat(monkeypatch):
    components, order = _setup(monkeypatch)
    assert run_daily_jobs(components, verbose=True) is True
    [checkin] = SpyCheckin.instances
    assert checkin.chat_id == "111"
    assert checkin.acc is components.accounts["shadow"]
    assert order == ["reflection", "checkin"]


def test_unpaired_telegram_skips_cleanly(monkeypatch, capsys):
    components, _ = _setup(monkeypatch, paired=False)
    assert run_daily_jobs(components, verbose=True) is True
    assert SpyCheckin.instances == []
    assert "[checkin:shadow] skipped" in capsys.readouterr().out


def test_failure_is_reported_for_retry_without_breaking_the_night(monkeypatch, capsys):
    components, _ = _setup(monkeypatch)
    SpyCheckin.fail = True
    assert run_daily_jobs(components) is False
    assert "[checkin:shadow] failed" in capsys.readouterr().err
