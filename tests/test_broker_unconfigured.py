"""setup-wizard T1: the placeholder broker that lets `serve` start with no
Alpaca credentials at all, so the first-run setup wizard is reachable in a
browser instead of the process exiting 2 before it can render."""

from __future__ import annotations

from decimal import Decimal

import pytest

from allpath_trade.broker.base import (
    Broker,
    BrokerError,
    BrokerNotConfigured,
    OrderIntent,
    OrderSide,
)
from allpath_trade.broker.unconfigured import UnconfiguredBroker
from allpath_trade.config import Settings

MESSAGE = "Alpaca keys are not set — finish setup"


def test_unconfigured_broker_is_a_broker_named_unconfigured():
    broker = UnconfiguredBroker()
    assert isinstance(broker, Broker)
    assert broker.name == "unconfigured"
    assert broker.is_paper is True


def test_broker_not_configured_is_a_broker_error():
    assert issubclass(BrokerNotConfigured, BrokerError)


@pytest.mark.parametrize("call", [
    lambda b: b.get_account(),
    lambda b: b.get_positions(),
    lambda b: b.get_order("o1"),
    lambda b: b.get_orders(),
    lambda b: b.get_orders(open_only=False),
    lambda b: b.submit_order(OrderIntent(ticker="AAPL", side=OrderSide.BUY,
                                         qty=Decimal(1), reason="x")),
    lambda b: b.cancel_order("o1"),
    lambda b: b.get_equity_history(30),
])
def test_every_method_raises_broker_not_configured_with_the_setup_message(call):
    with pytest.raises(BrokerNotConfigured) as exc:
        call(UnconfiguredBroker())
    assert str(exc.value) == MESSAGE


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "t.db",
                    strategies_dir=tmp_path / "strategies",
                    memory_dir=tmp_path / "memory", **kwargs)


def test_build_broker_returns_unconfigured_when_paper_has_no_keys(tmp_path):
    from allpath_trade.app import _build_broker

    settings = _settings(tmp_path, alpaca_api_key="", alpaca_secret_key="")
    broker = _build_broker("paper", settings, conn=None, data=None,
                           broker_override=None)
    assert isinstance(broker, UnconfiguredBroker)


@pytest.mark.parametrize("key,secret", [("", "s"), ("k", "")])
def test_build_broker_returns_unconfigured_when_either_key_is_missing(
        tmp_path, key, secret):
    from allpath_trade.app import _build_broker

    settings = _settings(tmp_path, alpaca_api_key=key, alpaca_secret_key=secret)
    broker = _build_broker("paper", settings, conn=None, data=None,
                           broker_override=None)
    assert isinstance(broker, UnconfiguredBroker)


def test_build_broker_returns_alpaca_when_both_keys_are_present(tmp_path, monkeypatch):
    import allpath_trade.broker.alpaca as alpaca_mod
    from allpath_trade.app import _build_broker

    built = {}

    class FakeAlpacaBroker:
        def __init__(self, api_key, secret_key, paper=True):
            built.update(api_key=api_key, secret_key=secret_key, paper=paper)

    monkeypatch.setattr(alpaca_mod, "AlpacaBroker", FakeAlpacaBroker)
    settings = _settings(tmp_path, alpaca_api_key="k", alpaca_secret_key="s")

    broker = _build_broker("paper", settings, conn=None, data=None,
                           broker_override=None)

    assert isinstance(broker, FakeAlpacaBroker)
    assert built == {"api_key": "k", "secret_key": "s", "paper": True}


def test_build_broker_override_still_wins_over_the_unconfigured_fallback(tmp_path):
    from allpath_trade.app import _build_broker

    sentinel = object()
    settings = _settings(tmp_path, alpaca_api_key="", alpaca_secret_key="")
    assert _build_broker("paper", settings, conn=None, data=None,
                         broker_override=sentinel) is sentinel
