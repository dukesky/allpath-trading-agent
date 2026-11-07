from decimal import Decimal

from allpath_trade.broker.base import Account, OptionIntent, OrderIntent, OrderSide, Position
from allpath_trade.risk.gate import RiskGate, RiskLimits, limits_for_account

ACCT = Account(equity=Decimal(10000), cash=Decimal(5000), buying_power=Decimal(10000))
AAPL_POS = Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(190),
                    market_value=Decimal(2000), unrealized_pl=Decimal(100))


def buy(notional="1000", ticker="AAPL"):
    return OrderIntent(ticker=ticker, side=OrderSide.BUY,
                       notional=Decimal(notional), reason="t")


def sell(qty="5", ticker="AAPL"):
    return OrderIntent(ticker=ticker, side=OrderSide.SELL, qty=Decimal(qty), reason="t")


def check(intent, limits=None, positions=None, trades_today=0, is_paper=True,
          price=Decimal(200), account=ACCT):
    gate = RiskGate(limits or RiskLimits())
    return gate.check(intent, account=account, positions=positions if positions is not None else [AAPL_POS],
                      trades_today=trades_today, is_paper=is_paper, price=price)


def test_approves_reasonable_buy():
    d = check(buy("1000"), positions=[])
    assert d.approved and d.reasons == []


def test_rejects_live_when_not_allowed():
    d = check(buy(), is_paper=False)
    assert not d.approved
    assert any("live" in r.lower() for r in d.reasons)


def test_allows_live_when_enabled():
    d = check(buy(), limits=RiskLimits(allow_live=True), is_paper=False, positions=[])
    assert d.approved


def test_rejects_order_value_above_cap():
    d = check(buy("6000"))
    assert not d.approved
    assert any("order value" in r.lower() for r in d.reasons)


def test_qty_order_value_uses_price():
    intent = OrderIntent(ticker="AAPL", side=OrderSide.BUY, qty=Decimal(30), reason="t")
    d = check(intent, price=Decimal(200))  # 30*200 = 6000 > 5000
    assert not d.approved


def test_rejects_daily_trade_limit():
    d = check(buy(), trades_today=10)
    assert not d.approved
    assert any("daily trade" in r.lower() for r in d.reasons)


def test_rejects_position_weight_breach():
    # AAPL 2000/10000 = 20%; buying 1000 more -> 30% > 25% cap
    d = check(buy("1000"))
    assert not d.approved
    assert any("position weight" in r.lower() for r in d.reasons)


def test_approves_buy_within_weight():
    d = check(buy("400"))  # -> 24%
    assert d.approved


def test_rejects_buy_breaking_cash_reserve():
    limits = RiskLimits(min_cash_reserve=Decimal(4500))
    d = check(buy("1000", ticker="MSFT"), limits=limits, positions=[])
    assert not d.approved
    assert any("cash reserve" in r.lower() for r in d.reasons)


def test_rejects_sell_exceeding_position():
    d = check(sell("11"))
    assert not d.approved
    assert any("exceeds position" in r.lower() for r in d.reasons)


def test_rejects_sell_of_unowned_ticker():
    d = check(sell("1", ticker="TSLA"))
    assert not d.approved


def test_collects_multiple_reasons():
    d = check(buy("6000"), trades_today=99)
    assert len(d.reasons) >= 2


def test_approves_order_value_exactly_at_cap():
    # order_value exactly at cap (using custom limit of 1000, not default 5000)
    # positions=[], ticker="MSFT" so weight rule doesn't interfere
    # weight = (0 + 1000) / 10000 = 10% < 25% ✓
    d = check(buy("1000", ticker="MSFT"), limits=RiskLimits(max_order_value=Decimal(1000)), positions=[])
    assert d.approved and d.reasons == []


def test_rejects_order_value_just_above_cap():
    # order_value just above cap
    d = check(buy("1000.01", ticker="MSFT"), limits=RiskLimits(max_order_value=Decimal(1000)), positions=[])
    assert not d.approved
    assert any("order value" in r.lower() for r in d.reasons)


def test_approves_weight_exactly_at_cap():
    # buy 500 with AAPL_POS (2000): (2000+500)/10000 = 25% exactly at cap
    d = check(buy("500"))
    assert d.approved and d.reasons == []


def test_approves_buy_leaving_exact_cash_reserve():
    # limits with min_cash_reserve=4000, buy 1000 ticker="MSFT" positions=[]
    # cash: 5000 - 1000 = 4000 == reserve (not below)
    d = check(buy("1000", ticker="MSFT"), limits=RiskLimits(min_cash_reserve=Decimal(4000)), positions=[])
    assert d.approved and d.reasons == []


def test_rejects_sell_notional_exceeding_position_value():
    # sell notional 2500 with AAPL_POS (value 2000): 2500 > 2000
    intent = OrderIntent(ticker="AAPL", side=OrderSide.SELL, notional=Decimal(2500), reason="t")
    d = check(intent)
    assert not d.approved
    assert any("exceeds position value" in r.lower() for r in d.reasons)


def test_approves_sell_notional_within_position_value():
    # sell notional 1500 with AAPL_POS (value 2000): 1500 <= 2000
    intent = OrderIntent(ticker="AAPL", side=OrderSide.SELL, notional=Decimal(1500), reason="t")
    d = check(intent)
    assert d.approved and d.reasons == []


def test_max_options_weight_defaults_to_10_percent():
    limits = RiskLimits()
    assert limits.max_options_weight == Decimal("0.10")


# -- check_option ------------------------------------------------------------

CALL_OCC = "META260918C00600000"
EXISTING_CALL_OCC = "META260918C00500000"


def opt_buy(occ=CALL_OCC, underlying="META", premium="500", qty=1):
    return OptionIntent(underlying=underlying, right="call", occ_symbol=occ,
                        side=OrderSide.BUY, qty=qty, est_premium=Decimal(premium), reason="t")


def opt_sell(occ=CALL_OCC, underlying="META", premium="0", qty=1):
    return OptionIntent(underlying=underlying, right="call", occ_symbol=occ,
                        side=OrderSide.SELL, qty=qty, est_premium=Decimal(premium), reason="t")


def check_option(intent, limits=None, positions=None, trades_today=0, is_paper=True,
                 account=ACCT):
    gate = RiskGate(limits or RiskLimits())
    return gate.check_option(intent, account=account,
                             positions=positions if positions is not None else [],
                             trades_today=trades_today, is_paper=is_paper)


def test_check_option_approves_reasonable_buy():
    d = check_option(opt_buy(premium="500"), positions=[])
    assert d.approved and d.reasons == []


def test_check_option_rejects_live_when_not_allowed():
    d = check_option(opt_buy(), is_paper=False)
    assert not d.approved
    assert any("live" in r.lower() for r in d.reasons)


def test_check_option_allows_live_when_enabled():
    d = check_option(opt_buy(premium="500"), limits=RiskLimits(allow_live=True),
                     is_paper=False, positions=[])
    assert d.approved


def test_check_option_rejects_premium_above_order_value_cap():
    d = check_option(opt_buy(premium="6000"), positions=[])
    assert not d.approved
    assert any("order value" in r.lower() for r in d.reasons)


def test_check_option_rejects_exposure_cap():
    # equity 10000, max_options_weight 0.10 -> cap 1000; existing 900 + new 200 = 1100 > cap
    positions = [Position(ticker=EXISTING_CALL_OCC, qty=Decimal(1), avg_entry_price=Decimal(9),
                          market_value=Decimal(900), unrealized_pl=Decimal(0))]
    d = check_option(opt_buy(premium="200"), positions=positions)
    assert not d.approved
    assert any("options exposure" in r.lower() for r in d.reasons)


def test_check_option_exposure_ignores_stock_positions():
    # A large stock position must not count toward the options exposure cap.
    positions = [Position(ticker="AAPL", qty=Decimal(10), avg_entry_price=Decimal(190),
                          market_value=Decimal(9000), unrealized_pl=Decimal(100))]
    d = check_option(opt_buy(premium="200"), positions=positions)
    assert d.approved and d.reasons == []


def test_check_option_approves_exposure_exactly_at_cap():
    positions = [Position(ticker=EXISTING_CALL_OCC, qty=Decimal(1), avg_entry_price=Decimal(8),
                          market_value=Decimal(800), unrealized_pl=Decimal(0))]
    d = check_option(opt_buy(premium="200"), positions=positions)  # 800 + 200 == 1000 cap
    assert d.approved and d.reasons == []


def test_check_option_sell_close_exempt_from_value_caps():
    d = check_option(opt_sell(premium="99999"), positions=[])
    assert d.approved and d.reasons == []


def test_check_option_rejects_daily_trade_limit_on_buy():
    d = check_option(opt_buy(premium="100"), positions=[], trades_today=10)
    assert not d.approved
    assert any("daily trade" in r.lower() for r in d.reasons)


def test_check_option_sell_exempt_from_daily_trade_limit():
    # Finding 2: a safety sweep/stop-loss close must never be blocked by the
    # shared daily-trade cap -- sells are fully cap-exempt now, not just
    # exempt from the value/exposure caps.
    d = check_option(opt_sell(premium="0"), positions=[], trades_today=10)
    assert d.approved and d.reasons == []


def test_check_option_exposure_skipped_when_equity_non_positive():
    # cash=1000 (not 0) isolates the exposure-cap-skip behavior under test
    # from Finding 3's new cash-reserve check (min_cash_reserve defaults to
    # 0, so any positive cash clears it) -- equity<=0 is what's exercised
    # here, not cash.
    zero_equity_acct = Account(equity=Decimal(0), cash=Decimal(1000), buying_power=Decimal(0))
    positions = [Position(ticker=EXISTING_CALL_OCC, qty=Decimal(1), avg_entry_price=Decimal(9999),
                          market_value=Decimal(999999), unrealized_pl=Decimal(0))]
    d = check_option(opt_buy(premium="100"), positions=positions, account=zero_equity_acct)
    assert d.approved and d.reasons == []


def test_check_option_collects_multiple_reasons():
    d = check_option(opt_buy(premium="6000"), positions=[], trades_today=99)
    assert len(d.reasons) >= 2


# -- Finding 3: min_cash_reserve for option buys -----------------------------

def test_check_option_rejects_buy_breaking_cash_reserve():
    limits = RiskLimits(min_cash_reserve=Decimal(4500))
    acct = Account(equity=Decimal(10000), cash=Decimal(5000), buying_power=Decimal(10000))
    d = check_option(opt_buy(premium="1000"), limits=limits, positions=[], account=acct)
    assert not d.approved
    assert any("cash reserve" in r.lower() for r in d.reasons)


def test_check_option_approves_buy_leaving_exact_cash_reserve():
    limits = RiskLimits(min_cash_reserve=Decimal(4000))
    acct = Account(equity=Decimal(10000), cash=Decimal(5000), buying_power=Decimal(10000))
    d = check_option(opt_buy(premium="1000"), limits=limits, positions=[], account=acct)
    assert d.approved and d.reasons == []


def test_check_option_sell_exempt_from_cash_reserve():
    limits = RiskLimits(min_cash_reserve=Decimal(9999999))
    d = check_option(opt_sell(premium="0"), limits=limits, positions=[])
    assert d.approved and d.reasons == []


# -- Finding 2: daily-trade cap exemption for sells (positive control) ------

def test_check_option_buy_still_rejects_daily_trade_limit():
    # Sanity check the buy side still respects the cap -- only sells are
    # exempt now.
    d = check_option(opt_buy(premium="100"), positions=[], trades_today=10)
    assert not d.approved
    assert any("daily trade" in r.lower() for r in d.reasons)


# --- per-account limit resolution (limits_for_account) -----------------------


class _FakeSettings:
    """Duck-typed stand-in for Settings — only the fields the resolver reads."""

    def __init__(self, **kw):
        self.max_order_value = Decimal(5000)
        self.max_position_weight = Decimal("0.25")
        self.max_options_weight = Decimal("0.10")
        self.max_daily_trades = 10
        self.min_cash_reserve = Decimal(0)
        self.shadow_max_order_value = None
        self.shadow_max_position_weight = None
        self.shadow_max_options_weight = None
        self.shadow_max_daily_trades = None
        self.shadow_min_cash_reserve = None
        for k, v in kw.items():
            setattr(self, k, v)


def test_limits_for_account_paper_uses_base_settings():
    lim = limits_for_account(_FakeSettings(), "paper")
    assert lim.max_order_value == Decimal(5000)
    assert lim.max_position_weight == Decimal("0.25")
    assert lim.max_daily_trades == 10


def test_limits_for_account_shadow_without_overrides_matches_base():
    s = _FakeSettings()
    assert limits_for_account(s, "shadow") == limits_for_account(s, "paper")


def test_limits_for_account_shadow_overrides_win():
    s = _FakeSettings(shadow_max_order_value=Decimal(30000),
                      shadow_max_position_weight=Decimal("0.40"))
    lim = limits_for_account(s, "shadow")
    assert lim.max_order_value == Decimal(30000)
    assert lim.max_position_weight == Decimal("0.40")
    # unoverridden fields still fall back to the base values
    assert lim.max_daily_trades == 10
    assert lim.max_options_weight == Decimal("0.10")


def test_shadow_overrides_never_leak_into_paper():
    s = _FakeSettings(shadow_max_order_value=Decimal(30000),
                      shadow_max_position_weight=Decimal("0.40"))
    lim = limits_for_account(s, "paper")
    assert lim.max_order_value == Decimal(5000)
    assert lim.max_position_weight == Decimal("0.25")


def test_zero_cash_reserve_override_is_honoured_not_treated_as_unset():
    """0 is a real value; only None means "no override"."""
    s = _FakeSettings(min_cash_reserve=Decimal(1000),
                      shadow_min_cash_reserve=Decimal(0))
    assert limits_for_account(s, "shadow").min_cash_reserve == Decimal(0)
    assert limits_for_account(s, "paper").min_cash_reserve == Decimal(1000)


# -- TDSC red-team F2: ticker allowlist ------------------------------------
# Allowed = the account's active strategy tickers + the configured universe.
# Only a human-approved order may trade anything else; a sell of a held
# position is always allowed (it can only shrink exposure).

def gate_with(tickers=("AAPL",), universe=()):
    return RiskGate(RiskLimits(), allowed_tickers=lambda: set(tickers), universe=universe)


def run(gate, intent, *, human_approved=False, positions=None):
    return gate.check(intent, account=ACCT,
                      positions=positions if positions is not None else [AAPL_POS],
                      trades_today=0, is_paper=True, price=Decimal(200),
                      human_approved=human_approved)


def test_allowlist_rejects_a_buy_outside_the_universe():
    d = run(gate_with(), buy("100", ticker="TQQQ"))
    assert not d.approved
    assert any("TQQQ is not in this account's trading universe" in r for r in d.reasons)


def test_allowlist_approves_a_strategy_ticker():
    assert run(gate_with(), buy("100")).approved


def test_allowlist_matches_case_insensitively():
    assert run(gate_with(tickers=("aapl",)), buy("100", ticker="AAPL")).approved


def test_configured_universe_extends_the_allowlist():
    assert run(gate_with(universe=("MSFT",)), buy("100", ticker="msft")).approved


def test_human_approved_order_may_trade_outside_the_universe():
    assert run(gate_with(), buy("100", ticker="TQQQ"), human_approved=True).approved


def test_sell_of_a_held_position_outside_the_universe_is_allowed():
    d = run(gate_with(tickers=()), sell("5"))
    assert d.approved


def test_unreadable_universe_fails_closed_for_agent_orders():
    def boom():
        raise RuntimeError("disk gone")
    gate = RiskGate(RiskLimits(), allowed_tickers=boom)
    d = run(gate, buy("100"))
    assert not d.approved
    assert any("could not read the trading universe" in r for r in d.reasons)
    assert run(gate, buy("100"), human_approved=True).approved


def test_gate_without_an_allowlist_provider_skips_the_symbol_check():
    # Unwired gates (unit tests, tools) keep the old behaviour; build_components
    # always wires one (see test_app).
    assert check(buy("100", ticker="TQQQ"), positions=[]).approved


# --- TDSC red-team F7: held sells are exempt from the per-order and daily caps

BIG_POS = Position(ticker="AAPL", qty=Decimal(100), avg_entry_price=Decimal(190),
                   market_value=Decimal(20000), unrealized_pl=Decimal(1000))


def test_sell_all_of_a_position_above_max_order_value_is_approved():
    # 100 * 200 = 20000 > max_order_value 5000: a sell-all stop must still fill.
    d = check(sell("100"), positions=[BIG_POS])
    assert d.approved, d.reasons


def test_sell_notional_above_max_order_value_is_approved_within_holding():
    intent = OrderIntent(ticker="AAPL", side=OrderSide.SELL,
                         notional=Decimal(15000), reason="t")
    d = check(intent, positions=[BIG_POS])
    assert d.approved, d.reasons


def test_held_sell_is_exempt_from_daily_trade_limit():
    d = check(sell("5"), trades_today=10)
    assert d.approved, d.reasons


def test_held_sell_still_bounded_by_position_size():
    d = check(sell("101"), positions=[BIG_POS])
    assert not d.approved
    assert any("exceeds position" in r for r in d.reasons)


def test_sell_of_unowned_ticker_still_hits_caps():
    # Not a held sell: the caps (and the no-position check) still apply.
    d = check(sell("100", ticker="MSFT"), trades_today=10)
    assert not d.approved
    assert any("daily trade" in r.lower() for r in d.reasons)
    assert any("order value" in r.lower() for r in d.reasons)


def test_buy_still_hits_caps():
    d = check(buy("6000"), trades_today=10)
    assert not d.approved
    assert any("order value" in r.lower() for r in d.reasons)
    assert any("daily trade" in r.lower() for r in d.reasons)
