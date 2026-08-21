from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from allpath_trade.broker.base import (
    Account,
    Broker,
    Order,
    OrderIntent,
    OrderSide,
    OrderStatus,
    Position,
)
from allpath_trade.data.base import DataSource
from allpath_trade.scheduler import today_et_date
from allpath_trade.store.db import LockedConnection

# Fractional-share and weighted-average-cost precision. Both a notional
# order's derived quantity (notional / price) and a weighted-average
# avg_cost ((old_qty*old_avg + new_qty*price) / total_qty) can produce a
# repeating decimal -- Decimal division rounds to the *context* precision
# (28 significant digits) rather than raising, which would make stored
# values effectively random-length and defeat the "exact string, no float
# artifacts" test goal. Quantizing both to a fixed 6dp keeps every stored
# number finite, reproducible, and fine enough for fractional-share
# bookkeeping (a broker's own fractional fills are typically 6dp or coarser).
_QTY_QUANT = Decimal("0.000001")
_AVG_COST_QUANT = Decimal("0.000001")


class ShadowLedger(Broker):
    """A local bookkeeping ledger, not a real broker connection.

    The shadow account mirrors a real brokerage the app has no API access
    to (e.g. Robinhood): the user trades there by hand, and tells the agent
    what happened so this ledger matches reality. `submit_order` therefore
    "fills" instantly at the current quote -- there is no order routing, no
    network call of any kind beyond the injected `DataSource` quote lookup,
    and never any real-brokerage credentials. Downstream copy (Task 7)
    makes this explicit to the user ("place this order in your brokerage
    now"); `is_paper=True` here just means "not real-money execution",
    consistent with the Broker base default -- the shadow/paper distinction
    itself is carried by `name`, not by this flag.

    The four `set_position`/`set_cash`/`remove_position`/`record_fill`
    methods below are NOT part of the `Broker` interface -- they are the
    only other way this ledger's tables are ever written, and only Task 6's
    human-approval applier calls them (never the agent directly). Every
    write path, `Broker.submit_order` included, appends an audit row to
    `shadow_orders`.
    """

    name = "shadow"
    # Not real-money execution -- this is a bookkeeping ledger, never a live
    # order route. The paper/shadow distinction downstream copy needs is
    # carried by `name` ("shadow"), not by this flag; Task 7 wires the
    # wording that tells the user to place the order at their real broker.
    is_paper = True

    def __init__(self, conn: LockedConnection, data: DataSource) -> None:
        self._conn = conn
        self._data = data

    # -- Broker interface -----------------------------------------------

    def get_account(self) -> Account:
        cash = self._get_cash()
        equity = cash
        for row in self._conn.execute("SELECT * FROM shadow_positions"):
            avg_cost = Decimal(row["avg_cost"])
            price = self._valuation_price(row["ticker"], row["last_price"], avg_cost)
            equity += Decimal(row["qty"]) * price
        try:
            self._upsert_equity_daily(equity, cash)
        except Exception:  # noqa: BLE001, S110 -- best-effort: a write failure
            pass  # must never break a read
        return Account(equity=equity, cash=cash, buying_power=cash)

    def get_positions(self) -> list[Position]:
        out = []
        for row in self._conn.execute("SELECT * FROM shadow_positions ORDER BY ticker"):
            qty = Decimal(row["qty"])
            avg_cost = Decimal(row["avg_cost"])
            price = self._valuation_price(row["ticker"], row["last_price"], avg_cost)
            out.append(Position(
                ticker=row["ticker"], qty=qty, avg_entry_price=avg_cost,
                market_value=qty * price, unrealized_pl=(price - avg_cost) * qty,
            ))
        return out

    def get_order(self, order_id: str) -> Order:
        row = self._get_order_row(order_id)
        if row is None:
            raise LookupError(f"no such shadow order: {order_id}")
        return self._row_to_order(row)

    def get_orders(self, open_only: bool = True) -> list[Order]:
        # Every shadow order fills or rejects synchronously inside
        # submit_order -- nothing is ever left "open" to report here.
        if open_only:
            return []
        rows = self._conn.execute(
            "SELECT * FROM shadow_orders WHERE side IN ('buy', 'sell') ORDER BY id DESC")
        return [self._row_to_order(r) for r in rows]

    def submit_order(self, intent: OrderIntent) -> Order:
        now = datetime.now(UTC)
        try:
            quote = self._data.get_quote(intent.ticker)
        except Exception:  # noqa: BLE001 -- a raising DataSource == no quote
            quote = None
        # No quote, no book entry at an unknown price -- reject outright,
        # nothing to record a fill price for and nothing to touch on
        # shadow_positions (a raising DataSource == "no quote").
        if quote is None:
            return self._record_order(
                intent, now, OrderStatus.REJECTED, qty=None, price=None,
                note=f"no quote available for {intent.ticker}")
        price = quote.price
        qty = self._resolve_qty(intent, price)
        with self._conn.transaction():
            if intent.side == OrderSide.BUY:
                order = self._fill_buy(intent, now, qty, price)
            else:
                order = self._fill_sell(intent, now, qty, price)
            # Refresh the ticker's last-known price even on a rejection
            # (insufficient cash / oversell) -- we did get a fresh quote,
            # so a position row that already exists for this ticker should
            # reflect it. No-op (rowcount 0) if no position row exists yet.
            self._conn.execute(
                "UPDATE shadow_positions SET last_price = ?, last_price_ts = ?"
                " WHERE ticker = ?",
                (str(price), now.isoformat(), intent.ticker))
        return order

    def cancel_order(self, order_id: str) -> None:
        # Nothing is ever "open" (see get_orders) -- there is no in-flight
        # order to cancel. A genuine no-op, not an error: callers that
        # unconditionally cancel a stale review-queue row must not blow up
        # just because the shadow account never has anything left to cancel.
        return None

    def get_equity_history(self, days: int) -> list[tuple[datetime, Decimal]]:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
        rows = self._conn.execute(
            "SELECT date, equity FROM shadow_equity_daily WHERE date >= ?"
            " ORDER BY date ASC", (cutoff,))
        return [
            (datetime.fromisoformat(r["date"]).replace(tzinfo=UTC), Decimal(r["equity"]))
            for r in rows
        ]

    # -- Ledger mutation helpers (Task 6's applier; never called by the
    # agent directly -- these are the human-approval-gated write path) ----

    def set_position(self, ticker: str, qty: Decimal, avg_cost: Decimal) -> None:
        ticker = ticker.strip().upper()
        now = datetime.now(UTC)
        with self._conn.transaction():
            existing = self._conn.execute(
                "SELECT qty, avg_cost FROM shadow_positions WHERE ticker = ?",
                (ticker,)).fetchone()
            before = f"{existing['qty']}@{existing['avg_cost']}" if existing else "none"
            self._conn.execute(
                "INSERT INTO shadow_positions"
                " (ticker, qty, avg_cost, last_price, last_price_ts, updated_ts)"
                " VALUES (?, ?, ?, NULL, NULL, ?)"
                " ON CONFLICT(ticker) DO UPDATE SET"
                " qty = excluded.qty, avg_cost = excluded.avg_cost,"
                " updated_ts = excluded.updated_ts",
                (ticker, str(qty), str(avg_cost), now.isoformat()))
            self._audit(now, ticker,
                       f"set_position {ticker}: {before} -> {qty}@{avg_cost}")

    def set_cash(self, amount: Decimal) -> None:
        now = datetime.now(UTC)
        with self._conn.transaction():
            before = self._get_cash()
            self._set_cash_raw(amount, now)
            self._audit(now, "", f"set_cash: {before} -> {amount}")

    def remove_position(self, ticker: str) -> None:
        ticker = ticker.strip().upper()
        now = datetime.now(UTC)
        with self._conn.transaction():
            existing = self._conn.execute(
                "SELECT qty, avg_cost FROM shadow_positions WHERE ticker = ?",
                (ticker,)).fetchone()
            before = f"{existing['qty']}@{existing['avg_cost']}" if existing else "none (no-op)"
            self._conn.execute("DELETE FROM shadow_positions WHERE ticker = ?", (ticker,))
            self._audit(now, ticker, f"remove_position {ticker}: was {before}")

    def record_fill(self, order_id: str, actual_price: Decimal) -> None:
        """Corrects a past FILLED order's fill price after the fact (e.g.
        the user reports what their real brokerage actually filled at).

        Math: revert this order's original effect, then re-apply it at
        `actual_price` -- rather than trying to patch the delta directly,
        which would double-count if the position has changed shape since
        (extra buys, partial sells) in ways that don't commute with a
        patched-in-place edit.

        BUY: the position's avg_cost is a running weighted average across
        every buy fill. Reverting this fill's contribution means computing
        the total cost basis the position would have had immediately
        *before* this fill: `old_total_value = current_qty*current_avg_cost
        - qty*old_price`. If the position has since been trimmed (sells)
        below this fill's original qty, `current_qty - qty` goes negative
        and that reversal is no longer meaningful -- raise ValueError
        rather than write a nonsensical position. Otherwise re-apply at the
        corrected price: `new_total_value = old_total_value + qty*actual_price`,
        divided back out over the same qty. Cash moves by the same
        revert-then-reapply logic: `+= qty*old_price` (refund), then
        `-= qty*actual_price` (recharge) -- net delta `qty*(old_price -
        actual_price)`. Cash is intentionally NOT guarded to stay
        non-negative here (unlike submit_order's insufficient-cash reject):
        this is a human-approved correction of the historical record, not a
        new trade decision the risk gate should second-guess.

        SELL: a sell fill only ever affects cash (the position's qty/
        avg_cost were already reduced at submit time and are independent of
        the price a share sold at), so only cash is reverted and reapplied:
        net delta `qty*(actual_price - old_price)`. There is no qty to
        revert, so no reversal-impossible case exists on this branch.
        """
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            raise ValueError(f"no such filled shadow order: {order_id}") from None
        with self._conn.transaction():
            row = self._conn.execute(
                "SELECT * FROM shadow_orders WHERE id = ? AND side IN ('buy', 'sell')"
                " AND status = 'filled'", (oid,)).fetchone()
            if row is None:
                raise ValueError(f"no such filled shadow order: {order_id}")
            now = datetime.now(UTC)
            ticker = row["ticker"]
            side = row["side"]
            qty = Decimal(row["qty"])
            old_price = Decimal(row["fill_price"])
            cash = self._get_cash()

            if side == OrderSide.BUY.value:
                pos = self._conn.execute(
                    "SELECT qty, avg_cost FROM shadow_positions WHERE ticker = ?",
                    (ticker,)).fetchone()
                current_qty = Decimal(pos["qty"]) if pos else Decimal(0)
                current_avg = Decimal(pos["avg_cost"]) if pos else Decimal(0)
                remaining_qty = current_qty - qty
                if remaining_qty < 0:
                    raise ValueError(
                        f"cannot correct order {order_id}: position {ticker} now holds"
                        f" only {current_qty}, less than this fill's {qty} -- reversal"
                        " would go negative")
                old_total_value = current_qty * current_avg - qty * old_price
                new_total_value = old_total_value + qty * actual_price
                if remaining_qty == 0:
                    # This fill was the entire position; re-adding it back
                    # restores the same qty at the corrected price.
                    new_avg = actual_price.quantize(_AVG_COST_QUANT, rounding=ROUND_HALF_UP)
                else:
                    new_avg = (new_total_value / current_qty).quantize(
                        _AVG_COST_QUANT, rounding=ROUND_HALF_UP)
                self._conn.execute(
                    "INSERT INTO shadow_positions"
                    " (ticker, qty, avg_cost, last_price, last_price_ts, updated_ts)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(ticker) DO UPDATE SET"
                    " qty = excluded.qty, avg_cost = excluded.avg_cost,"
                    " updated_ts = excluded.updated_ts",
                    (ticker, str(current_qty), str(new_avg), str(actual_price),
                     now.isoformat(), now.isoformat()))
                new_cash = cash + qty * old_price - qty * actual_price
            else:
                new_avg = None
                new_cash = cash - qty * old_price + qty * actual_price

            self._set_cash_raw(new_cash, now)
            self._conn.execute(
                "UPDATE shadow_orders SET fill_price = ? WHERE id = ?",
                (str(actual_price), oid))
            avg_note = f", avg_cost -> {new_avg}" if new_avg is not None else ""
            self._audit(
                now, ticker,
                f"record_fill order #{oid} ({side}, qty {qty}):"
                f" fill_price {old_price} -> {actual_price}{avg_note}")

    # -- internals ---------------------------------------------------------

    def _resolve_qty(self, intent: OrderIntent, price: Decimal) -> Decimal:
        if intent.qty is not None:
            return intent.qty
        return (intent.notional / price).quantize(_QTY_QUANT, rounding=ROUND_HALF_UP)

    def _fill_buy(self, intent: OrderIntent, now: datetime, qty: Decimal,
                  price: Decimal) -> Order:
        cost = qty * price
        cash = self._get_cash()
        if cost > cash:
            return self._record_order(
                intent, now, OrderStatus.REJECTED, qty, price,
                note=f"insufficient cash: need {cost}, have {cash}")
        existing = self._conn.execute(
            "SELECT qty, avg_cost FROM shadow_positions WHERE ticker = ?",
            (intent.ticker,)).fetchone()
        if existing is None:
            new_qty, new_avg = qty, price
        else:
            old_qty = Decimal(existing["qty"])
            old_avg = Decimal(existing["avg_cost"])
            new_qty = old_qty + qty
            new_avg = ((old_qty * old_avg + qty * price) / new_qty).quantize(
                _AVG_COST_QUANT, rounding=ROUND_HALF_UP)
        self._conn.execute(
            "INSERT INTO shadow_positions"
            " (ticker, qty, avg_cost, last_price, last_price_ts, updated_ts)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(ticker) DO UPDATE SET"
            " qty = excluded.qty, avg_cost = excluded.avg_cost,"
            " last_price = excluded.last_price, last_price_ts = excluded.last_price_ts,"
            " updated_ts = excluded.updated_ts",
            (intent.ticker, str(new_qty), str(new_avg), str(price), now.isoformat(),
             now.isoformat()))
        self._set_cash_raw(cash - cost, now)
        return self._record_order(intent, now, OrderStatus.FILLED, qty, price, note="")

    def _fill_sell(self, intent: OrderIntent, now: datetime, qty: Decimal,
                   price: Decimal) -> Order:
        existing = self._conn.execute(
            "SELECT qty, avg_cost FROM shadow_positions WHERE ticker = ?",
            (intent.ticker,)).fetchone()
        held = Decimal(existing["qty"]) if existing else Decimal(0)
        if qty > held:
            return self._record_order(
                intent, now, OrderStatus.REJECTED, qty, price,
                note=f"oversell: requested {qty}, have {held}")
        remaining = held - qty
        if remaining == 0:
            self._conn.execute(
                "DELETE FROM shadow_positions WHERE ticker = ?", (intent.ticker,))
        else:
            self._conn.execute(
                "UPDATE shadow_positions SET qty = ?, last_price = ?, last_price_ts = ?,"
                " updated_ts = ? WHERE ticker = ?",
                (str(remaining), str(price), now.isoformat(), now.isoformat(),
                 intent.ticker))
        self._set_cash_raw(self._get_cash() + qty * price, now)
        return self._record_order(intent, now, OrderStatus.FILLED, qty, price, note="")

    def _record_order(self, intent: OrderIntent, now: datetime, status: OrderStatus,
                      qty: Decimal | None, price: Decimal | None, note: str) -> Order:
        filled = status == OrderStatus.FILLED
        notional_col = str(intent.notional) if intent.notional is not None else None
        qty_col = str(qty) if qty is not None else (
            str(intent.qty) if intent.qty is not None else None)
        with self._conn.transaction():
            row = self._conn.execute(
                "INSERT INTO shadow_orders"
                " (ts, ticker, side, qty, notional, status, fill_price, filled_at, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (now.isoformat(), intent.ticker, intent.side.value, qty_col, notional_col,
                 status.value, str(price) if filled else None,
                 now.isoformat() if filled else None, note))
        return Order(
            id=str(row.lastrowid), ticker=intent.ticker, side=intent.side,
            qty=intent.qty, notional=intent.notional, status=status,
            filled_qty=(qty if filled and qty is not None else Decimal(0)),
            filled_avg_price=(price if filled else None),
            submitted_at=now, filled_at=(now if filled else None),
        )

    def _audit(self, now: datetime, ticker: str, note: str) -> None:
        self._conn.execute(
            "INSERT INTO shadow_orders"
            " (ts, ticker, side, qty, notional, status, fill_price, filled_at, note)"
            " VALUES (?, ?, 'adjust', NULL, NULL, 'adjusted', NULL, NULL, ?)",
            (now.isoformat(), ticker, note))

    def _get_order_row(self, order_id: str):
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return None
        return self._conn.execute(
            "SELECT * FROM shadow_orders WHERE id = ? AND side IN ('buy', 'sell')",
            (oid,)).fetchone()

    def _row_to_order(self, row) -> Order:
        side = OrderSide(row["side"])
        status = OrderStatus(row["status"])
        notional = Decimal(row["notional"]) if row["notional"] is not None else None
        qty = None if notional is not None else (
            Decimal(row["qty"]) if row["qty"] is not None else None)
        filled_qty = (
            Decimal(row["qty"])
            if status == OrderStatus.FILLED and row["qty"] is not None
            else Decimal(0))
        filled_avg_price = Decimal(row["fill_price"]) if row["fill_price"] is not None else None
        filled_at = datetime.fromisoformat(row["filled_at"]) if row["filled_at"] else None
        return Order(
            id=str(row["id"]), ticker=row["ticker"], side=side, qty=qty, notional=notional,
            status=status, filled_qty=filled_qty, filled_avg_price=filled_avg_price,
            submitted_at=datetime.fromisoformat(row["ts"]), filled_at=filled_at,
        )

    def _get_cash(self) -> Decimal:
        row = self._conn.execute("SELECT cash FROM shadow_cash WHERE id = 1").fetchone()
        return Decimal(row["cash"]) if row is not None else Decimal(0)

    def _set_cash_raw(self, cash: Decimal, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO shadow_cash (id, cash, updated_ts) VALUES (1, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " cash = excluded.cash, updated_ts = excluded.updated_ts",
            (str(cash), now.isoformat()))

    def _valuation_price(self, ticker: str, last_price: str | None,
                         avg_cost: Decimal) -> Decimal:
        """Fresh quote if available, else the last price this ledger itself
        recorded (staleness surfaced to the user via last_price_ts -- Task 7
        renders it), else the cost basis as a last resort. Deliberately does
        NOT write last_price/last_price_ts back to shadow_positions here --
        only submit_order does that -- so a plain valuation read never has
        the side effect of silently refreshing the staleness timestamp it is
        itself supposed to be honest about."""
        try:
            return self._data.get_quote(ticker).price
        except Exception:  # noqa: BLE001, S110 -- a raising DataSource means
            pass  # fall through to last_price/avg_cost below
        if last_price is not None:
            return Decimal(last_price)
        return avg_cost

    def _upsert_equity_daily(self, equity: Decimal, cash: Decimal) -> None:
        today = today_et_date()
        self._conn.execute(
            "INSERT INTO shadow_equity_daily (date, equity, cash) VALUES (?, ?, ?)"
            " ON CONFLICT(date) DO UPDATE SET"
            " equity = excluded.equity, cash = excluded.cash",
            (today, str(equity), str(cash)))
        self._conn.commit()
