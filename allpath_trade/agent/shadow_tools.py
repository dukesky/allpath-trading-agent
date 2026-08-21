"""Shadow-ledger editing tools (shadow-dual-active T6, spec §④).

The shadow ledger's ONLY write paths are `Broker.submit_order` (via the risk
gate, same as paper) and the four `ShadowLedger` mutation helpers
(`set_position`/`set_cash`/`remove_position`/`record_fill`) -- and per T3's
own docstring, those four are "the human-approval-gated write path", never
called by the agent directly. This module is the tool layer that lets the
shadow-account agent PROPOSE a ledger edit; the actual mutation always goes
through either a human `confirm()` (terminal) or the `pending_reviews`
approval queue (web/Telegram, `kind="shadow_edit"` -- see
`store/reviews.py`'s `add_shadow_edit`/`_approve_shadow_edit`).

`register_shadow_tools` is wired ONLY into the shadow account's tool
registries (`web/chat_service.py`'s `ChatService._build` when
`self.account == "shadow"`, and `cli.py`'s `cmd_chat` under the same
condition) -- never paper's, never reflection's. There is no ledger to edit
on the paper account (Alpaca is the broker of record there), so the tools
simply don't exist in that registry at all -- not merely unused.

Snapshot helpers (`position_snapshot`/`cash_snapshot`/`order_snapshot`/
`full_snapshot`) are the ONE normalized read of "the ledger's current state
relevant to this op", shared by the tool functions (which record a `before`
snapshot at propose time) and `apply_shadow_edit_factory`'s applier (which
re-reads the same shape at approval time to detect staleness) -- so the two
can never disagree about what "unchanged since proposal" means.
"""

from __future__ import annotations

import csv
import io
import re
import sys
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from allpath_trade.notify import events
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.dispatch import notify_review_queued
from allpath_trade.store.app_state import AppState
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError

if TYPE_CHECKING:
    from allpath_trade.agent.tools import ToolRegistry
    from allpath_trade.broker.shadow import ShadowLedger
    from allpath_trade.store.db import LockedConnection

# Deliberately looser than a real exchange's symbol rules (no attempt to
# validate against a real ticker universe -- this ledger has no such data)
# but tight enough to reject garbage: 1-10 chars, starts with a letter,
# uppercase letters/digits/dot/hyphen only (covers share classes like
# "BRK.B" and OTC-style hyphenated tickers) -- matches the shape every
# ticker already entering this app (OrderIntent, PositionPlan) is silently
# assumed to have, just enforced explicitly here since a shadow edit is
# free-text human/LLM input with no order-routing broker to reject it first.
_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def _valid_ticker(raw: str) -> str | None:
    ticker = raw.strip().upper()
    return ticker if _TICKER_RE.match(ticker) else None


# --- Ledger snapshot helpers (read-only) ------------------------------------

def position_snapshot(ledger: ShadowLedger, ticker: str) -> dict | None:
    """The CURRENT state of `ticker`'s position, or `None` if there isn't
    one -- qty/avg_cost only (never last_price: a live quote tick must never
    look like a ledger change to the staleness check)."""
    ticker = ticker.strip().upper()
    for p in ledger.get_positions():
        if p.ticker == ticker:
            return {"qty": str(p.qty), "avg_cost": str(p.avg_entry_price)}
    return None


def cash_snapshot(ledger: ShadowLedger) -> dict:
    return {"cash": str(ledger.get_account().cash)}


def order_snapshot(ledger: ShadowLedger, order_id: str) -> dict | None:
    """The CURRENT state of a filled buy/sell order this ledger recorded, or
    `None` if `order_id` doesn't resolve to one (unknown id, an 'adjust'
    audit row, or an order that never filled) -- `record_fill` can only ever
    correct a filled order, so "not found" and "not filled" collapse to the
    same signal here."""
    try:
        order = ledger.get_order(str(order_id))
    except LookupError:
        return None
    if order.status.value != "filled":
        return None
    return {"ticker": order.ticker, "side": order.side.value,
            "qty": str(order.filled_qty), "fill_price": str(order.filled_avg_price)}


def full_snapshot(ledger: ShadowLedger) -> dict:
    """The whole book: every position plus cash -- used by the CSV
    import/reset ops (which touch more than one ticker at once) as their
    `before`/staleness-comparison shape."""
    positions = {p.ticker: {"qty": str(p.qty), "avg_cost": str(p.avg_entry_price)}
                for p in ledger.get_positions()}
    return {"positions": positions, "cash": str(ledger.get_account().cash)}


def preview_import(before: dict, rows: list[dict], cash: Decimal | None) -> dict:
    """Read-only preview of what `full_snapshot` will look like after an
    "import" op applies -- upsert semantics (mirrors `ShadowLedger.
    set_position`'s own ON CONFLICT upsert): every row replaces/creates that
    ticker's position, tickers not mentioned are left alone, and cash only
    changes if the CSV carried a CASH row at all."""
    positions = dict(before.get("positions", {}))
    for row in rows:
        positions[row["ticker"]] = {"qty": row["qty"], "avg_cost": row["avg_cost"]}
    new_cash = str(cash) if cash is not None else before.get("cash", "0")
    return {"positions": positions, "cash": new_cash}


# --- CSV import parsing ------------------------------------------------------

_CSV_HEADER = ("ticker", "qty", "avg_cost")


def parse_shadow_csv(text: str) -> tuple[list[dict], Decimal | None, list[str]]:
    """Parses+validates a `ticker,qty,avg_cost` CSV (an optional single
    `CASH,<amount>` row sets cash; an optional header row matching
    `_CSV_HEADER` is skipped). Returns `(rows, cash, errors)` -- `rows`/
    `cash` are only meaningful when `errors` is empty; a single bad line
    reports itself with its 1-indexed line number and parsing continues so
    every error in the file is reported at once, but the caller must queue
    NOTHING at all if `errors` is non-empty (spec: "解析+校验每一行,任何
    错误都不入队,列出行号")."""
    reader = csv.reader(io.StringIO(text))
    all_rows = list(reader)
    errors: list[str] = []
    rows: list[dict] = []
    cash: Decimal | None = None
    seen: set[str] = set()

    start = 0
    if all_rows and tuple(c.strip().lower() for c in all_rows[0][:3]) == _CSV_HEADER:
        start = 1

    for i, raw in enumerate(all_rows[start:], start=start + 1):
        cells = [c.strip() for c in raw]
        if not cells or all(c == "" for c in cells):
            continue
        if cells[0].upper() == "CASH":
            if len(cells) != 2:
                errors.append(f"line {i}: CASH row must have exactly 2 columns "
                              "(CASH,<amount>)")
                continue
            if cash is not None:
                errors.append(f"line {i}: only one CASH row is allowed")
                continue
            try:
                amount = Decimal(cells[1])
            except InvalidOperation:
                errors.append(f"line {i}: invalid cash amount {cells[1]!r}")
                continue
            if amount < 0:
                errors.append(f"line {i}: cash must be >= 0")
                continue
            cash = amount
            continue

        if len(cells) != 3:
            errors.append(f"line {i}: expected 3 columns (ticker,qty,avg_cost), "
                          f"got {len(cells)}")
            continue
        ticker_raw, qty_raw, avg_cost_raw = cells
        ticker = _valid_ticker(ticker_raw)
        if ticker is None:
            errors.append(f"line {i}: invalid ticker {ticker_raw!r}")
            continue
        if ticker in seen:
            errors.append(f"line {i}: duplicate ticker {ticker}")
            continue
        try:
            qty = Decimal(qty_raw)
        except InvalidOperation:
            errors.append(f"line {i}: invalid qty {qty_raw!r}")
            continue
        if qty < 0:
            errors.append(f"line {i}: qty must be >= 0")
            continue
        try:
            avg_cost = Decimal(avg_cost_raw)
        except InvalidOperation:
            errors.append(f"line {i}: invalid avg_cost {avg_cost_raw!r}")
            continue
        if avg_cost <= 0:
            errors.append(f"line {i}: avg_cost must be > 0")
            continue
        seen.add(ticker)
        rows.append({"ticker": ticker, "qty": str(qty), "avg_cost": str(avg_cost)})

    if not rows and cash is None and not errors:
        errors.append("no rows found in the file")
    return rows, cash, errors


# --- Approval applier (wired via ReviewQueue.set_shadow_edit_applier) ------

def _stale_check(current: object, before: object) -> None:
    if current != before:
        raise RevisionValidationError(
            "ledger changed since this proposal — re-propose")


def apply_shadow_edit_factory(
        ledger: ShadowLedger,
        conn: LockedConnection) -> Callable[[str, dict, object], None]:
    """Builds the applier `ReviewQueue.set_shadow_edit_applier` calls when a
    `shadow_edit` row is approved -- the ONE place an approved ledger edit
    ever actually reaches `ShadowLedger`'s mutation helpers. `conn` is the
    SAME connection object `ledger` itself was constructed with (identity,
    not just equality) -- `conn.transaction()` wrapping several ledger calls
    nests via savepoints with the ledger's own internal `with self._conn.
    transaction():` blocks (see `LockedConnection.transaction`'s docstring
    on nesting, and `ShadowLedger._upsert_equity_daily`'s comment
    anticipating exactly this caller), giving the "import"/"reset" ops
    atomicity across multiple rows: any row failing rolls back everything
    written so far in this apply, not just that one row.

    Every op re-reads the ledger's CURRENT relevant state and compares it to
    the row's recorded `before` (the same snapshot helpers the propose-time
    tool used) before writing anything -- a mismatch means the book moved
    since this proposal was queued, and raises `RevisionValidationError`
    (caught by `ReviewQueue._approve_shadow_edit`, which rolls the row back
    to "pending" rather than leaving it stuck "approved" with nothing
    applied). A bad/missing arg (`KeyError`, `InvalidOperation`) or a
    `ShadowLedger` mutation helper's own `ValueError` (e.g. `record_fill`'s
    intervening-sell guard) is caught the same way -- all fail the row back
    to pending with the message surfaced, never leaving it half-applied."""

    def apply(op: str, args: dict, before: object) -> None:
        try:
            if op == "set_position":
                _stale_check(position_snapshot(ledger, args["ticker"]), before)
                with conn.transaction():
                    ledger.set_position(
                        args["ticker"], Decimal(args["qty"]), Decimal(args["avg_cost"]))
            elif op == "set_cash":
                _stale_check(cash_snapshot(ledger), before)
                with conn.transaction():
                    ledger.set_cash(Decimal(args["amount"]))
            elif op == "remove_position":
                _stale_check(position_snapshot(ledger, args["ticker"]), before)
                with conn.transaction():
                    ledger.remove_position(args["ticker"])
            elif op == "record_fill":
                _stale_check(order_snapshot(ledger, args["order_id"]), before)
                with conn.transaction():
                    ledger.record_fill(args["order_id"], Decimal(args["actual_price"]))
            elif op == "import":
                _stale_check(full_snapshot(ledger), before)
                with conn.transaction():
                    for row in args["rows"]:
                        ledger.set_position(
                            row["ticker"], Decimal(row["qty"]), Decimal(row["avg_cost"]))
                    if args.get("cash") is not None:
                        ledger.set_cash(Decimal(args["cash"]))
            elif op == "reset":
                current = full_snapshot(ledger)
                _stale_check(current, before)
                with conn.transaction():
                    for ticker in current["positions"]:
                        ledger.remove_position(ticker)
                    ledger.set_cash(Decimal(0))
            else:
                raise RevisionValidationError(f"unknown shadow edit op {op!r}")
        except RevisionValidationError:
            raise
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise RevisionValidationError(str(exc)) from exc

    return apply


# --- Chat tools --------------------------------------------------------------

def register_shadow_tools(registry: ToolRegistry, *, ledger: ShadowLedger,
                          queue: ReviewQueue | None,
                          conversation_id_fn: Callable[[], int | None],
                          confirm: Callable[[str], bool],
                          notifier: Notifier | None = None,
                          web_base_url: str = "",
                          app_state: AppState | None = None,
                          telegram_bot_token: str = "") -> None:
    """Registers the four shadow-ledger editing tools. `queue` is the mode
    discriminator (same pattern as `action_tools.register_action_tools`'s
    `draft_strategy`): given, every tool queues a `shadow_edit` approval
    row and returns immediately, never touching `ledger`; `None` (terminal),
    every tool blocks on `confirm()` and, if accepted, calls the matching
    `ShadowLedger` mutation helper directly -- there is no approval queue in
    a terminal session, so a human typing y/N right there IS the approval."""

    def _notify_queued(review_id: int, action_title: str) -> None:
        # Best-effort, never breaks the proposal -- same reasoning as
        # action_tools.py's `_notify_chat_draft_queued`: the row is already
        # committed by the time this runs.
        if notifier is None and not telegram_bot_token:
            return
        try:
            approve_url = events.approve_link(web_base_url, review_id)
            subject, body = events.review_queued(
                review_id=review_id, ticker="", action=action_title,
                strategy_id="", approve_url=approve_url)
            notify_review_queued(
                queue=queue, notifier=notifier, app_state=app_state,
                telegram_bot_token=telegram_bot_token, review_id=review_id,
                subject=subject, body=body)
        except Exception as exc:  # noqa: BLE001 — see docstring above
            print(f"[shadow_tools] queued-edit notification failed: {exc}",
                  file=sys.stderr)

    def _queue_or_confirm(*, op: str, ticker: str, action_title: str, args: dict,
                          before: object, after: object, confirm_prompt: str,
                          apply_direct: Callable[[], None]) -> str:
        if queue is not None:
            try:
                rid = queue.add_shadow_edit(
                    op=op, ticker=ticker, action=action_title, args=args,
                    before=before, after=after,
                    conversation_id=conversation_id_fn())
            except Exception as exc:  # noqa: BLE001 — spec 降级: a queue
                # failure must return an error string, not silently drop
                # the requested edit.
                return f"error: could not queue ledger change: {exc}"
            _notify_queued(rid, action_title)
            return (f"Ledger change queued for your approval as #{rid} — open "
                    "Pending (or tap the link in the notification). It will "
                    "not take effect until you approve it.")
        if not confirm(confirm_prompt):
            return "user declined"
        try:
            apply_direct()
        except ValueError as exc:
            return f"error: {exc}"
        return f"applied: {action_title}"

    def shadow_set_position(ticker: str, qty: str, avg_cost: str) -> str:
        ticker_u = _valid_ticker(ticker)
        if ticker_u is None:
            return f"error: invalid ticker format {ticker!r}"
        try:
            qty_d = Decimal(str(qty))
        except InvalidOperation:
            return f"error: invalid qty {qty!r}"
        if qty_d < 0:
            return "error: qty must be >= 0"
        try:
            avg_cost_d = Decimal(str(avg_cost))
        except InvalidOperation:
            return f"error: invalid avg_cost {avg_cost!r}"
        if avg_cost_d <= 0:
            return "error: avg_cost must be > 0"

        before = position_snapshot(ledger, ticker_u)
        after = {"qty": str(qty_d), "avg_cost": str(avg_cost_d)}
        title = f"Set position {ticker_u}"
        return _queue_or_confirm(
            op="set_position", ticker=ticker_u, action_title=title,
            args={"ticker": ticker_u, "qty": str(qty_d), "avg_cost": str(avg_cost_d)},
            before=before, after=after,
            confirm_prompt=f"{title}: {before or 'no position'} -> {after}?",
            apply_direct=lambda: ledger.set_position(ticker_u, qty_d, avg_cost_d))

    def shadow_set_cash(amount: str) -> str:
        try:
            amount_d = Decimal(str(amount))
        except InvalidOperation:
            return f"error: invalid amount {amount!r}"
        if amount_d < 0:
            return "error: cash must be >= 0"

        before = cash_snapshot(ledger)
        after = {"cash": str(amount_d)}
        title = "Set cash"
        return _queue_or_confirm(
            op="set_cash", ticker="", action_title=title,
            args={"amount": str(amount_d)}, before=before, after=after,
            confirm_prompt=f"{title}: {before} -> {after}?",
            apply_direct=lambda: ledger.set_cash(amount_d))

    def shadow_remove_position(ticker: str) -> str:
        ticker_u = _valid_ticker(ticker)
        if ticker_u is None:
            return f"error: invalid ticker format {ticker!r}"
        before = position_snapshot(ledger, ticker_u)
        if before is None:
            return f"error: no position in {ticker_u} to remove"
        title = f"Remove position {ticker_u}"
        return _queue_or_confirm(
            op="remove_position", ticker=ticker_u, action_title=title,
            args={"ticker": ticker_u}, before=before, after=None,
            confirm_prompt=f"{title}: {before} -> none?",
            apply_direct=lambda: ledger.remove_position(ticker_u))

    def shadow_record_fill(order_id: str, actual_price: str) -> str:
        try:
            price_d = Decimal(str(actual_price))
        except InvalidOperation:
            return f"error: invalid actual_price {actual_price!r}"
        if price_d <= 0:
            return "error: actual_price must be > 0"

        before = order_snapshot(ledger, order_id)
        if before is None:
            return f"error: no filled shadow order #{order_id} to correct"
        after = {"fill_price": str(price_d)}
        title = f"Correct fill #{order_id}"
        return _queue_or_confirm(
            op="record_fill", ticker=before["ticker"], action_title=title,
            args={"order_id": str(order_id), "actual_price": str(price_d)},
            before=before, after=after,
            confirm_prompt=(f"{title}: fill price {before['fill_price']} -> "
                            f"{price_d} (avg cost recalculated on approval)?"),
            apply_direct=lambda: ledger.record_fill(str(order_id), price_d))

    t = "string"
    registry.register(
        "shadow_set_position",
        "Set a position in the shadow ledger (your real-brokerage mirror) to "
        "an exact quantity and average cost -- use this to match what you "
        "actually hold (a manual buy/sell at your real broker, or a "
        "first-time import). In web/Telegram chat this queues the edit for "
        "your approval on the Pending page; in a terminal session you "
        "confirm inline before it's applied. Never invents a position: "
        "always reflects what you tell it.",
        {"type": "object", "properties": {
            "ticker": {"type": t}, "qty": {"type": t}, "avg_cost": {"type": t}},
         "required": ["ticker", "qty", "avg_cost"]},
        shadow_set_position)
    registry.register(
        "shadow_set_cash",
        "Set the shadow ledger's cash balance to an exact amount -- use this "
        "to match your real brokerage's cash. Same approval flow as "
        "shadow_set_position.",
        {"type": "object", "properties": {"amount": {"type": t}},
         "required": ["amount"]},
        shadow_set_cash)
    registry.register(
        "shadow_remove_position",
        "Remove a position from the shadow ledger entirely (you no longer "
        "hold it). Same approval flow as shadow_set_position.",
        {"type": "object", "properties": {"ticker": {"type": t}},
         "required": ["ticker"]},
        shadow_remove_position)
    registry.register(
        "shadow_record_fill",
        "Correct a past shadow order's recorded fill price to what your "
        "real brokerage actually filled at -- the position's average cost "
        "is recalculated from the correction. Only works on an order that "
        "hasn't been affected by a later sell or manual adjustment on that "
        "ticker. Same approval flow as shadow_set_position.",
        {"type": "object", "properties": {
            "order_id": {"type": t}, "actual_price": {"type": t}},
         "required": ["order_id", "actual_price"]},
        shadow_record_fill)
