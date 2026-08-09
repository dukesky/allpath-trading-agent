from __future__ import annotations

import concurrent.futures
import re
import time
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from allpath_trade.app import Components
from allpath_trade.broker.base import Position
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.strategy.model import RuleState, RuleType, StrategyDoc
from allpath_trade.web.templating import templates

router = APIRouter()

BROKER_TIMEOUT_SECONDS = 10
# A dedicated, small pool -- not FastAPI/Starlette's own sync-handler
# threadpool. A sync route handler already runs in that shared pool, which
# every other page's request also needs a worker from; a broker call that
# just hangs (a phone that keeps reloading against a stalled Alpaca
# connection) would occupy one of those workers indefinitely, and enough
# concurrent hangs exhaust the pool and take the whole app down with it —
# login, chat, reviews, everything. Submitting to this pool instead means a
# hang can only ever consume *this* pool's capacity, and `.result(timeout=)`
# lets the request thread give up and return the request promptly either
# way; the submitted call keeps running in the background rather than being
# killed (Python has no way to force that), but nothing here waits on it.
_broker_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="dashboard-broker")


def nav_context(components: Components) -> dict:
    return {"pending_count": len(components.queue.list())}


def _with_timeout(fn):
    # Reads the module attribute at call time, not as a bound default
    # argument -- a default value is captured once at function-definition
    # time, which would make BROKER_TIMEOUT_SECONDS un-monkeypatchable
    # (tests need to shrink it to prove the bound is actually enforced,
    # without waiting out the real production timeout).
    return _broker_pool.submit(fn).result(timeout=BROKER_TIMEOUT_SECONDS)


_QUOTE_CACHE_TTL_SECONDS = 60
# Module-level so it survives across requests within the process (not just
# within one page render) -- every dashboard reload would otherwise hit
# yfinance once per strategy, and a phone that auto-refreshes hammers it.
# Keyed by ticker; value is (fetched_at, Quote | None) -- a failed lookup is
# cached too, so a ticker yfinance can't resolve doesn't get retried on
# every single reload either.
_quote_cache: dict[str, tuple[float, Quote | None]] = {}


def _cached_quote(data: DataSource, ticker: str) -> Quote | None:
    now = time.monotonic()
    cached = _quote_cache.get(ticker)
    if cached is not None and now - cached[0] < _QUOTE_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        found = _with_timeout(lambda: data.get_quote(ticker))
    except Exception:  # noqa: BLE001 — a quote failure must render "—", never an error page
        found = None
    _quote_cache[ticker] = (now, found)
    return found


# Deliberately shallow: matches the first "price <op> number" it finds in a
# rule's condition text. Conditions like "rsi < 30" or compound boolean
# expressions ("price < 205 and position_weight < target_weight" -- the
# first comparison still matches here, which is intentional: the leading
# price check is the one worth surfacing as a key level) are handled
# best-effort; anything with no "price" comparison at all is omitted rather
# than guessed.
_LEVEL_RE = re.compile(r"price\s*([<>]=?)\s*([\d.]+)")


def _key_level(rule) -> str | None:
    """Label a rule's price level using only the three combinations the
    product brief calls out explicitly: a hard sell rule below price is a
    stop, a sell rule above price is a target, a buy rule below price is an
    add zone. Every other combination (soft sell below, buy above, an
    action that mentions neither buy nor sell) is omitted rather than
    guessed."""
    match = _LEVEL_RE.search(rule.condition)
    if not match:
        return None
    op, value = match.group(1), match.group(2)
    action = rule.action.lower()
    below = op.startswith("<")
    if rule.type == RuleType.HARD and "sell" in action and below:
        label = "stop"
    elif "sell" in action and not below:
        label = "target"
    elif "buy" in action and below:
        label = "add zone"
    else:
        return None
    return f"{label} {op} {value}"


def summarize_strategy(doc: StrategyDoc, position: Position | None, quote: Quote | None, *,
                        equity: Decimal | None = None, has_pending: bool = False) -> dict:
    """Pure function -- no I/O, no template lookups. The dashboard card
    renders only this dict, so the card's content is testable without
    parsing HTML. Callers do the I/O (broker position lookup, cached quote
    fetch, pending-review lookup) and pass the results in."""
    key_levels = [level for r in doc.rules if (level := _key_level(r)) is not None]

    current_weight_pct = None
    if position is not None and equity:
        current_weight_pct = float(position.market_value / equity * 100)

    target_weight_pct = None
    if doc.position.target_weight is not None:
        target_weight_pct = float(doc.position.target_weight * 100)

    alerts = []
    if any(r.state == RuleState.TRIGGERED for r in doc.rules):
        alerts.append("rule triggered")
    if has_pending:
        alerts.append("pending review")

    return {
        "id": doc.id,
        "ticker": doc.position.ticker,
        "name": doc.name,
        "status": doc.status.value,
        "auth": doc.authorization.value,
        "key_levels": key_levels,
        "current_weight_pct": current_weight_pct,
        "target_weight_pct": target_weight_pct,
        "price": quote.price if quote is not None else None,
        # Quote (allpath_trade/data/base.py) carries only a last price, no
        # previous close -- there is nothing to compare against, so
        # direction degrades to neutral rather than being invented.
        "price_class": "",
        "alerts": alerts,
    }


def error_redirect(target: str, message: str | None = None) -> RedirectResponse:
    """The shared "303 back to a page, error riding along as a query
    param" idiom. Every POST-triggered failure across the app (reviews.py's
    approve/reject, strategies.py's rearm) is hit by a plain
    `<form method="post">`, not an htmx partial swap — there is no element
    in the current page for a bare HTML fragment to swap into, so a route
    can't just return one directly without stranding the user with no nav
    and no way back. Redirecting (303, so the browser re-issues as GET)
    keeps the page coherent either way; `message`, when given, is rendered
    at the top of whichever page `target` is."""
    if message:
        return RedirectResponse(f"{target}?error={quote(message)}", status_code=303)
    return RedirectResponse(target, status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()
    account = None
    positions: list = []
    broker_error = ""
    try:
        account = _with_timeout(c.broker.get_account)
        positions = _with_timeout(c.broker.get_positions)
    except concurrent.futures.TimeoutError:
        broker_error = f"Broker unavailable: timed out after {BROKER_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 — a broker outage must not blank the page
        broker_error = f"Broker unavailable: {exc}"

    errors: list[str] = []
    strategies = c.strategies.load_all(status=None, errors=errors)
    positions_by_ticker = {p.ticker: p for p in positions}
    pending_strategy_ids = {row["strategy_id"] for row in c.queue.list("pending")}
    equity = account.equity if account is not None else None
    strategy_cards = [
        summarize_strategy(
            s, positions_by_ticker.get(s.position.ticker),
            _cached_quote(c.data, s.position.ticker),
            equity=equity, has_pending=s.id in pending_strategy_ids)
        for s in strategies
    ]
    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard", "account": account, "positions": positions,
        "broker_error": broker_error, "strategy_cards": strategy_cards,
        "strategy_errors": errors, "trades": c.journal.recent(limit=8),
        **nav_context(c)})
