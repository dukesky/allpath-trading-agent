from __future__ import annotations

from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide as _Side
from alpaca.trading.enums import QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from tradewind.broker.base import (
    Account,
    Broker,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)

# Alpaca order statuses collapsed onto our coarse OrderStatus
_STATUS_MAP = {
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
}


class AlpacaBroker(Broker):
    name = "alpaca"

    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 client: object | None = None) -> None:
        self.is_paper = paper
        self._client = client or TradingClient(api_key, secret_key, paper=paper)

    def get_account(self) -> Account:
        a = self._client.get_account()
        return Account(equity=Decimal(a.equity), cash=Decimal(a.cash),
                       buying_power=Decimal(a.buying_power))

    def get_positions(self) -> list[Position]:
        return [
            Position(ticker=p.symbol, qty=Decimal(p.qty),
                     avg_entry_price=Decimal(p.avg_entry_price),
                     market_value=Decimal(p.market_value),
                     unrealized_pl=Decimal(p.unrealized_pl))
            for p in self._client.get_all_positions()
        ]

    def get_order(self, order_id: str) -> Order:
        return self._to_order(self._client.get_order_by_id(order_id))

    def get_orders(self, open_only: bool = True) -> list[Order]:
        status = QueryOrderStatus.OPEN if open_only else QueryOrderStatus.ALL
        raw = self._client.get_orders(filter=GetOrdersRequest(status=status))
        return [self._to_order(o) for o in raw]

    def submit_order(self, intent: OrderIntent) -> Order:
        req = MarketOrderRequest(
            symbol=intent.ticker,
            side=_Side.BUY if intent.side == OrderSide.BUY else _Side.SELL,
            time_in_force=TimeInForce.DAY,
            qty=float(intent.qty) if intent.qty is not None else None,
            notional=float(intent.notional) if intent.notional is not None else None,
        )
        return self._to_order(self._client.submit_order(req))

    def cancel_order(self, order_id: str) -> None:
        self._client.cancel_order_by_id(order_id)

    @staticmethod
    def _to_order(o: object) -> Order:
        status = _STATUS_MAP.get(str(o.status.value), OrderStatus.SUBMITTED)
        return Order(
            id=str(o.id),
            ticker=o.symbol,
            side=OrderSide(o.side.value),
            qty=Decimal(o.qty) if o.qty is not None else None,
            notional=Decimal(o.notional) if o.notional is not None else None,
            status=status,
            filled_qty=Decimal(o.filled_qty or "0"),
            filled_avg_price=Decimal(o.filled_avg_price) if o.filled_avg_price else None,
            submitted_at=o.submitted_at,
        )
