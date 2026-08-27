"""Unit tests for the sync `OptionsBackend` facade over Alpaca's MCP
server, plus one deselected-by-default live integration test.

Unit tests never spawn the real `uvx alpaca-mcp-server` process: they
exercise `_parse_result` directly (a static method) and `pick_contract` /
`place_option_order` through a fake subclass that overrides `_call` with
canned JSON shaped exactly like the payloads probed live against the real
server (see docs/superpowers/specs/2026-08-27-options-via-mcp-design.md).

Run the integration test with:
    uv run pytest -m integration tests/test_options_mcp.py -v
"""
from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from allpath_trade.broker.options_mcp import (
    McpOptionsBackend,
    OptionPick,
    OptionsBackendError,
)
from allpath_trade.config import Settings


# ---------------------------------------------------------------------------
# _parse_result: envelope / error handling
# ---------------------------------------------------------------------------

class TestParseResult:
    def test_parses_data_envelope(self):
        text = (
            '{"_alpaca_mcp_security": {"trust": "untrusted_tool_output"}, '
            '"data": {"option_contracts": []}}'
        )
        assert McpOptionsBackend._parse_result(text) == {"option_contracts": []}

    def test_error_text_raises_options_backend_error(self):
        text = "Error calling tool 'get_option_contracts': something broke"
        with pytest.raises(OptionsBackendError) as exc_info:
            McpOptionsBackend._parse_result(text)
        assert text == str(exc_info.value)

    def test_malformed_json_propagates(self):
        with pytest.raises(Exception):
            McpOptionsBackend._parse_result("not json")


# ---------------------------------------------------------------------------
# Fake backend: overrides `_call` so pick_contract/place_option_order run
# their real selection logic against canned data without any subprocess,
# thread, or event loop.
# ---------------------------------------------------------------------------

class _FakeBackend(McpOptionsBackend):
    def __init__(self, responses: dict[str, dict]):
        # Deliberately skip McpOptionsBackend.__init__: no Settings, no
        # thread/lock machinery needed since _call is fully overridden.
        self._responses = responses
        self.calls: list[tuple[str, dict]] = []

    def _call(self, tool: str, args: dict) -> dict:
        self.calls.append((tool, args))
        if tool not in self._responses:
            raise AssertionError(f"unexpected tool call: {tool}({args})")
        return self._responses[tool]


def _contract(symbol: str, expiration_date: str, strike_price: str, tradable: bool = True) -> dict:
    return {
        "id": "fake-id",
        "symbol": symbol,
        "name": f"fake {symbol}",
        "status": "active",
        "tradable": tradable,
        "expiration_date": expiration_date,
        "root_symbol": symbol[:4],
        "underlying_symbol": symbol[:4],
        "type": "call" if "C" in symbol else "put",
        "style": "american",
        "strike_price": strike_price,
        "multiplier": "100",
        "size": "100",
        "open_interest": None,
        "open_interest_date": None,
        "close_price": None,
        "close_price_date": None,
        "ppind": True,
    }


def _quote_response(occ_symbol: str, ap: float) -> dict:
    return {"quotes": {occ_symbol: {"ap": ap, "as": 1, "ax": "N", "bp": ap - 1, "bs": 1, "bx": "A", "c": "A", "t": "2026-08-27T19:56:48Z"}}}


# ---------------------------------------------------------------------------
# pick_contract selection math
# ---------------------------------------------------------------------------

class TestPickContract:
    def test_picks_nearest_expiry_and_closest_strike_for_call(self):
        near = "2026-09-11"
        far = "2026-09-18"
        contracts = [
            _contract("META260911C00090000", near, "90"),
            _contract("META260911C00100000", near, "100"),
            _contract("META260911C00110000", near, "110"),
            # Farther expiry, closer strike -- must still lose to `near`.
            _contract("META260918C00100000", far, "100"),
        ]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            "get_option_latest_quote": _quote_response("META260911C00100000", 5.0),
        })

        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.02"), budget=Decimal("1000"), spot=Decimal("100"),
        )

        assert pick is not None
        # target strike = 100 * 1.02 = 102 -> closest among {90,100,110} is 100
        assert pick.occ_symbol == "META260911C00100000"
        assert pick.expiry == date(2026, 9, 11)
        assert pick.strike == Decimal("100")
        assert pick.ask == Decimal("5.0")
        assert pick.qty == 2  # floor(1000 / (5*100)) = 2
        assert pick.est_premium == Decimal("1000.0")

    def test_picks_closest_strike_below_spot_for_put(self):
        expiry = "2026-09-11"
        contracts = [
            _contract("META260911P00090000", expiry, "90"),
            _contract("META260911P00095000", expiry, "95"),
            _contract("META260911P00100000", expiry, "100"),
        ]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            "get_option_latest_quote": _quote_response("META260911P00095000", 3.0),
        })

        pick = backend.pick_contract(
            underlying="META", right="put", min_dte=7,
            otm_pct=Decimal("0.05"), budget=Decimal("1000"), spot=Decimal("100"),
        )

        assert pick is not None
        # target strike = 100 * (1 - 0.05) = 95 -> exact match
        assert pick.occ_symbol == "META260911P00095000"
        assert pick.strike == Decimal("95")

    def test_filters_non_tradable_contracts(self):
        expiry = "2026-09-11"
        contracts = [
            _contract("META260911C00100000", expiry, "100", tradable=False),
            _contract("META260911C00110000", expiry, "110", tradable=True),
        ]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            "get_option_latest_quote": _quote_response("META260911C00110000", 4.0),
        })

        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.0"), budget=Decimal("1000"), spot=Decimal("100"),
        )

        assert pick is not None
        assert pick.occ_symbol == "META260911C00110000"

    def test_no_contracts_returns_none(self):
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": []},
        })
        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.02"), budget=Decimal("1000"), spot=Decimal("100"),
        )
        assert pick is None

    def test_all_non_tradable_returns_none(self):
        expiry = "2026-09-11"
        contracts = [_contract("META260911C00100000", expiry, "100", tradable=False)]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
        })
        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.02"), budget=Decimal("1000"), spot=Decimal("100"),
        )
        assert pick is None

    def test_unaffordable_budget_returns_none(self):
        expiry = "2026-09-11"
        contracts = [_contract("META260911C00100000", expiry, "100")]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            # ask*100 = 500, budget = 400 -> qty = 0
            "get_option_latest_quote": _quote_response("META260911C00100000", 5.0),
        })
        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.0"), budget=Decimal("400"), spot=Decimal("100"),
        )
        assert pick is None

    def test_zero_ask_returns_none(self):
        expiry = "2026-09-11"
        contracts = [_contract("META260911C00100000", expiry, "100")]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            "get_option_latest_quote": _quote_response("META260911C00100000", 0.0),
        })
        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.0"), budget=Decimal("10000"), spot=Decimal("100"),
        )
        assert pick is None

    def test_missing_quote_returns_none(self):
        expiry = "2026-09-11"
        contracts = [_contract("META260911C00100000", expiry, "100")]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            "get_option_latest_quote": {"quotes": {}},
        })
        pick = backend.pick_contract(
            underlying="META", right="call", min_dte=7,
            otm_pct=Decimal("0.0"), budget=Decimal("10000"), spot=Decimal("100"),
        )
        assert pick is None

    def test_uses_expiration_bound_and_expected_contract_filters(self):
        expiry = "2026-09-11"
        contracts = [_contract("META260911C00100000", expiry, "100")]
        backend = _FakeBackend({
            "get_option_contracts": {"option_contracts": contracts},
            "get_option_latest_quote": _quote_response("META260911C00100000", 5.0),
        })
        backend.pick_contract(
            underlying="META", right="call", min_dte=10,
            otm_pct=Decimal("0.02"), budget=Decimal("1000"), spot=Decimal("100"),
        )

        tool, args = backend.calls[0]
        assert tool == "get_option_contracts"
        assert args["underlying_symbols"] == "META"
        assert args["type"] == "call"
        assert args["expiration_date_gte"] == (datetime.now(UTC).date() + timedelta(days=10)).isoformat()
        assert args["limit"] == 300

        tool2, args2 = backend.calls[1]
        assert tool2 == "get_option_latest_quote"
        assert args2["symbols"] == "META260911C00100000"


# ---------------------------------------------------------------------------
# place_option_order
# ---------------------------------------------------------------------------

class TestPlaceOptionOrder:
    def test_forwards_expected_args(self):
        backend = _FakeBackend({
            "place_option_order": {"id": "order-1", "status": "accepted"},
        })
        result = backend.place_option_order(
            occ_symbol="META260911C00100000", side="buy", qty=2,
            position_intent="buy_to_open",
        )
        assert result == {"id": "order-1", "status": "accepted"}
        tool, args = backend.calls[0]
        assert tool == "place_option_order"
        assert args["symbol"] == "META260911C00100000"
        assert args["side"] == "buy"
        assert args["qty"] == "2"  # STRING, not int
        assert isinstance(args["qty"], str)
        assert args["position_intent"] == "buy_to_open"
        assert args["type"] == "market"
        assert args["time_in_force"] == "day"


# ---------------------------------------------------------------------------
# OptionPick model
# ---------------------------------------------------------------------------

class TestOptionPick:
    def test_construction(self):
        pick = OptionPick(
            occ_symbol="META260911C00100000", expiry=date(2026, 9, 11),
            strike=Decimal("100"), ask=Decimal("5.0"), qty=2,
            est_premium=Decimal("1000.0"),
        )
        assert pick.occ_symbol == "META260911C00100000"


# ---------------------------------------------------------------------------
# Live integration test (deselected by default -- same mechanism as
# tests/test_broker_alpaca_integration.py). Spawns the real
# `uvx alpaca-mcp-server`, lists tools, and fetches one contract + one
# quote, read-only. Never places an order.
#
# Pinned live probe (2026-08-27, against the real server):
#   get_option_contracts -> data["option_contracts"][i] has keys:
#     id, symbol, name, status, tradable, expiration_date, root_symbol,
#     underlying_symbol, underlying_asset_id, type, style, strike_price
#     (numeric string, e.g. "100"), multiplier, size, open_interest,
#     open_interest_date, close_price, close_price_date, ppind.
#   get_option_latest_quote -> data["quotes"][occ_symbol] has keys:
#     ap (ask price, float), as, ax, bp, bs, bx, c, t.
# This matches the fixtures used by the fake-backend tests above.
# ---------------------------------------------------------------------------

_REPO_ROOT_ENV = "/Users/tianzhang/Projects/allpath-trading-agent/.env"


def _live_settings() -> Settings | None:
    if not os.path.exists(_REPO_ROOT_ENV):
        return None
    settings = Settings(_env_file=_REPO_ROOT_ENV)
    if not (settings.alpaca_api_key and settings.alpaca_secret_key):
        return None
    return settings


@pytest.mark.integration
def test_live_mcp_server_contracts_and_quote():
    settings = _live_settings()
    if settings is None:
        pytest.skip("no Alpaca keys available (repo-root .env missing or empty)")

    backend = McpOptionsBackend(settings)
    try:
        data = backend._call("get_option_contracts", {
            "underlying_symbols": "META",
            "type": "call",
            "expiration_date_gte": (datetime.now(UTC).date() + timedelta(days=7)).isoformat(),
            "limit": 5,
        })
        contracts = data["option_contracts"]
        assert contracts
        first = contracts[0]
        for key in ("symbol", "expiration_date", "strike_price", "tradable"):
            assert key in first

        occ = first["symbol"]
        quote_data = backend._call("get_option_latest_quote", {"symbols": occ})
        assert occ in quote_data["quotes"]
        assert "ap" in quote_data["quotes"][occ]
    finally:
        backend.stop()


@pytest.mark.integration
def test_live_mcp_server_lists_place_option_order_tool():
    settings = _live_settings()
    if settings is None:
        pytest.skip("no Alpaca keys available (repo-root .env missing or empty)")

    import asyncio

    backend = McpOptionsBackend(settings)
    try:
        with backend._lock:
            if backend._session is None:
                backend._spawn_locked()
            future = asyncio.run_coroutine_threadsafe(
                backend._session.list_tools(), backend._loop,
            )
            tools = future.result(timeout=30)
        names = [t.name for t in tools.tools]
        assert "place_option_order" in names
    finally:
        backend.stop()
