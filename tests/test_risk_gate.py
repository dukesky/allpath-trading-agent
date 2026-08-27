from decimal import Decimal

from allpath_trade.broker.base import Account, OrderIntent, OrderSide, Position
from allpath_trade.risk.gate import RiskGate, RiskLimits

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
