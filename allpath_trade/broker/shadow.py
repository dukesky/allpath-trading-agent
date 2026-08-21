from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
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
# The rounding this introduces is bounded and tiny: ROUND_HALF_UP at 6dp is
# off by at most 0.0000005 per unit, so a $20k notional order's derived qty
# (and the avg_cost a buy folds into the position) drifts by at most
# roughly $0.001 -- far below a single cent on any position size this
# ledger deals in.
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
            # No quote, no book entry at an unknown price -- reject outright,
            # nothing to record a fill price for and nothing to touch on
            # shadow_positions. `get_quote`'s contract always returns a
            # `Quote` or raises (never a bare `None`), so handling this
            # straight from the except -- rather than funneling it through a
            # `quote = None` sentinel checked afterward -- covers the one
            # real "no quote" case without a second branch that can never
            # otherwise be reached.
            return self._record_order(
                intent, now, OrderStatus.REJECTED, qty=None, price=None,
                note=f"no quote available for {intent.ticker}")
        price = quote.price
        qty = self._resolve_qty(intent, price)
        with self._conn.transaction():
            if qty <= 0:
                # A notional order that rounds to less than one 6dp share at
                # the current price (e.g. a fraction-of-a-cent notional
                # against a normal share price) -- reject outright rather
                # than writing a 0-share FILLED order and a phantom
                # shadow_positions row for a purchase that never actually
                # bought anything.
                note = (
                    f"notional ${intent.notional} too small at price ${price}"
                    if intent.notional is not None else f"invalid order quantity {qty}")
                order = self._record_order(
                    intent, now, OrderStatus.REJECTED, qty=None, price=None, note=note)
            elif intent.side == OrderSide.BUY:
                order = self._fill_buy(intent, now, qty, price)
            else:
                order = self._fill_sell(intent, now, qty, price)
            # Refresh the ticker's last-known price even on a rejection
            # (insufficient cash / oversell / too-small-notional) -- we did
            # get a fresh quote, so a position row that already exists for
            # this ticker should reflect it. No-op (rowcount 0) if no
            # position row exists yet.
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
        # Anchor "today" to the same ET calendar date `_upsert_equity_daily`
        # stamps rows with (`today_et_date()`), not a raw UTC-now
        # truncation -- otherwise a call made late evening ET but already
        # past UTC midnight (or the reverse, early UTC morning but still
        # the prior ET day) computes a cutoff one calendar day off from the
        # dates actually stored in shadow_equity_daily.
        today = date.fromisoformat(today_et_date())
        cutoff = (today - timedelta(days=days)).isoformat()
        rows = self._conn.execute(
            "SELECT date, equity FROM shadow_equity_daily WHERE date >= ?"
            " ORDER BY date ASC", (cutoff,))
        out = []
        for r in rows:
            equity = Decimal(r["equity"])
            if equity == 0:
                # Pre-funding / never-written days report equity as exactly
                # 0 -- not a real data point. Base Broker.get_equity_history's
                # contract (and AlpacaBroker's implementation) already
                # filters these; match that parity here.
                continue
            out.append((datetime.fromisoformat(r["date"]).replace(tzinfo=UTC), equity))
        return out

    # -- Ledger mutation helpers (Task 6's applier; never called by the
    # agent directly -- these are the human-approval-gated write path) ----

    def set_position(self, ticker: str, qty: Decimal, avg_cost: Decimal) -> None:
        # No silent shorts: a shadow position mirrors real holdings the
        # user actually has, never a short thesis this ledger has no
        # borrow/margin model to represent. avg_cost must be strictly
        # positive too -- a zero or negative cost basis isn't a real
        # correction, just corrupt input. (Cash going negative is a
        # different question, deliberately left unguarded -- see set_cash
        # below.)
        if qty < 0:
            raise ValueError(f"set_position: qty must be >= 0 (got {qty})")
        if avg_cost <= 0:
            raise ValueError(f"set_position: avg_cost must be > 0 (got {avg_cost})")
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
        # Deliberately no non-negative guard here (unlike set_position's
        # qty/avg_cost guards above) -- this covers cash only. A shadow
        # account's cash going negative (e.g. correcting toward a margin or
        # debit balance) is a real state Task 6's human-approval applier
        # gets to decide about, not something this low-level ledger
        # primitive should reject outright.
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

        Guard: `remaining_qty < 0` alone isn't enough to catch every case
        where reverting-then-reapplying no longer means what it claims to.
        Two examples: (a) buy, buy a second lot, sell part of the combined
        position, then correct the FIRST buy -- `current_qty` still exceeds
        this fill's qty, so `remaining_qty >= 0`, but the shares the sell
        liquidated could well have been (some of) this very fill's, and
        there is no per-lot tracking to tell. (b) a position is sold to
        zero and later reopened by an unrelated buy -- `current_qty` can
        coincidentally land back at or above the old fill's qty, at which
        point this correction would silently rewrite a brand new,
        unrelated position's avg_cost. Both are caught below by refusing to
        correct a fill if ANY sell fill or manual set_position/
        remove_position adjustment has touched this ticker since -- those
        are exactly the events that break the "nothing else changed this
        position's shape" assumption the revert math depends on.
        Re-correcting THIS SAME order again is explicitly exempted: it
        doesn't change the position's shape, only this order's own recorded
        price, so chaining corrections on one order stays safe and doesn't
        trip its own audit trail.
        """
        if actual_price <= 0:
            raise ValueError(
                f"cannot correct order {order_id} to fill price {actual_price}:"
                " price must be positive")
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

            # Guard before any write: any filled sell, or any manual
            # set_position/remove_position/record_fill adjustment on this
            # ticker with a later order id, means the book has moved in a
            # way this correction can't safely unwind. A prior record_fill
            # correction of THIS SAME order is exempted (it wrote its own
            # 'adjust' audit row, identifiable by its fixed note prefix)
            # since re-correcting one order repeatedly is safe.
            self_correction_prefix = f"record_fill order #{oid} ("
            blockers = self._conn.execute(
                "SELECT side, note FROM shadow_orders"
                " WHERE ticker = ? AND id > ?"
                " AND ((status = 'filled' AND side = 'sell') OR side = 'adjust')",
                (ticker, oid)).fetchall()
            for b in blockers:
                if b["side"] == "adjust" and b["note"].startswith(self_correction_prefix):
                    continue
                raise ValueError(
                    f"cannot correct order {order_id}: {ticker} has been sold or"
                    " manually adjusted since this fill -- fix the book with"
                    " set_position instead")

            if side == OrderSide.BUY.value:
                pos = self._conn.execute(
                    "SELECT qty, avg_cost FROM shadow_positions WHERE ticker = ?",
                    (ticker,)).fetchone()
                current_qty = Decimal(pos["qty"]) if pos else Decimal(0)
                current_avg = Decimal(pos["avg_cost"]) if pos else Decimal(0)
                remaining_qty = current_qty - qty
                if remaining_qty < 0:
                    # Belt-and-suspenders: the guard above should already
                    # have refused any ticker with an intervening sell (the
                    # only way remaining_qty could go negative with no
                    # other buys involved), so this is not expected to be
                    # reachable in practice -- kept as a defensive invariant
                    # check rather than trusting the guard's SQL alone.
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
                # The guard above guarantees no sell or manual adjustment
                # has touched this ticker since this fill, so a position row
                # is always present here -- a plain UPDATE, never a fresh
                # INSERT. record_fill corrects a historical fill price, not
                # the current market price, so last_price/last_price_ts are
                # deliberately left untouched (unlike _fill_buy's write).
                self._conn.execute(
                    "UPDATE shadow_positions SET qty = ?, avg_cost = ?, updated_ts = ?"
                    " WHERE ticker = ?",
                    (str(current_qty), str(new_avg), now.isoformat(), ticker))
                new_cash = cash + qty * old_price - qty * actual_price
            else:
                new_avg = None
                new_cash = cash - qty * old_price + qty * actual_price

            self._set_cash_raw(new_cash, now)
            # Keep the audit row's notional truthful too: a notional-based
            # order's `notional` column holds the actual dollar amount
            # transacted, which changes when the fill price is corrected. A
            # qty-based order never had a notional value and keeps it NULL.
            new_notional = str(qty * actual_price) if row["notional"] is not None else None
            self._conn.execute(
                "UPDATE shadow_orders SET fill_price = ?, notional = ? WHERE id = ?",
                (str(actual_price), new_notional, oid))
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
        # `self._conn.transaction()`, not a bare `execute` + `commit()`: a
        # bare `commit()` commits (and closes out) the underlying sqlite
        # connection's transaction unconditionally, including any SAVEPOINT
        # an outer `transaction()` call currently has open on this same
        # thread (e.g. Task 6's applier doing snapshot-then-mutate inside
        # one outer transaction that calls get_account() along the way).
        # That outer call's later ROLLBACK TO/RELEASE then targets a
        # savepoint that no longer exists and raises OperationalError,
        # masking whatever real error triggered the rollback and leaving
        # this partial write committed underneath it.  `transaction()`
        # nests safely via depth-keyed savepoints and only actually commits
        # at depth 0, so it's correct both standalone (called directly from
        # get_account) and nested inside a caller's own transaction().
        today = today_et_date()
        with self._conn.transaction():
            self._conn.execute(
                "INSERT INTO shadow_equity_daily (date, equity, cash) VALUES (?, ?, ?)"
                " ON CONFLICT(date) DO UPDATE SET"
                " equity = excluded.equity, cash = excluded.cash",
                (today, str(equity), str(cash)))
