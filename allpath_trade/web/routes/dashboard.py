from __future__ import annotations

import concurrent.futures
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from allpath_trade.app import Components
from allpath_trade.broker.base import Position
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.scheduler import is_market_hours
from allpath_trade.store.app_state import SENTINEL_HEARTBEAT_KEY, SENTINEL_MARKET_OPEN_KEY
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

# One shared budget for *all* quote lookups in a single dashboard render, not
# BROKER_TIMEOUT_SECONDS per strategy -- a cold cache against N strategies
# with an unresponsive data source would otherwise cost up to
# N * BROKER_TIMEOUT_SECONDS on top of the two broker calls above. A module
# attribute, read at call time rather than a bound default, for the same
# reason as BROKER_TIMEOUT_SECONDS: tests need to shrink it to prove the
# budget is enforced without waiting out the real value.
QUOTES_BUDGET_SECONDS = 10


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

    # Quote.previous_close (allpath_trade/data/base.py) is optional -- a
    # data source that can't supply it (or a quote lookup that failed
    # entirely) leaves it None. Only when both price and a non-zero
    # previous_close are present is there anything to compare against;
    # otherwise direction degrades to neutral rather than being invented,
    # and no percentage is rendered at all.
    day_change_pct = None
    price_class = ""
    if quote is not None and quote.price is not None and quote.previous_close:
        day_change_pct = float((quote.price - quote.previous_close) / quote.previous_close * 100)
        if day_change_pct > 0:
            price_class = "up"
        elif day_change_pct < 0:
            price_class = "down"

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
        "day_change_pct": day_change_pct,
        "price_class": price_class,
        "alerts": alerts,
    }


def _utcnow() -> datetime:
    # A plain module-level function, not a bound default argument, so tests
    # can monkeypatch it to a frozen instant (dashboard_route._utcnow) the
    # same way the scheduler tests patch its clock -- that suite already
    # burned once on an unpatched clock (_is_after_close in test_scheduler.py).
    return datetime.now(UTC)


def _parse_heartbeat_ts(raw: str) -> datetime:
    """Parse a heartbeat timestamp written by AppState.set.

    AppState always writes `datetime.now(UTC).isoformat()`, but tolerate a
    naive value (interpreted as UTC, matching every other timestamp in this
    codebase -- see fmt.ago) or a non-UTC offset (converted to UTC) rather
    than assuming the exact representation that happens to be written today.
    """
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def sentinel_heartbeat_status(raw: str | None, interval_minutes: int, *,
                              market_open_raw: str | None = None,
                              now: datetime | None = None) -> dict:
    """Pure function -- takes already-read `app_state.get(...)` values, no
    I/O, so it's testable without a store (same shape as summarize_strategy
    above). A malformed stored value degrades to the same "never ran" copy
    as a missing one, rather than 500ing the dashboard.

    `market_open_raw` is the SENTINEL_MARKET_OPEN_KEY value written
    alongside the heartbeat timestamp (scheduler.py's `_run_sentinel_pass`).
    The scheduler ticks -- and writes the heartbeat -- on every interval,
    market open or closed; only the *evaluation* is gated on market hours.
    Saying "last check Nm ago" when the last tick landed outside market
    hours (a weekend, after close) is a claim nothing was actually checked.
    `market_open_raw is None` covers a heartbeat written before this field
    existed -- treated as open, i.e. today's copy, rather than guessing.
    The staleness warning still keys off tick age either way: the tick
    itself is real proof of life regardless of whether the market was open
    when it landed."""
    if raw is None:
        return {"text": "Sentinel: never ran (daemon not running?)", "warn": False}
    try:
        last = _parse_heartbeat_ts(raw)
    except (TypeError, ValueError):
        return {"text": "Sentinel: never ran (daemon not running?)", "warn": False}
    elapsed = (now or _utcnow()) - last
    minutes = max(0, int(elapsed.total_seconds() // 60))
    warn = minutes > 2 * interval_minutes
    if market_open_raw == "false":
        text = f"Sentinel: monitoring paused (market closed) · last tick {minutes}m ago"
    else:
        text = f"Sentinel: last check {minutes}m ago · interval {interval_minutes}m"
    return {"text": text, "warn": warn}


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


def notice_redirect(target: str, message: str) -> RedirectResponse:
    """Same POST-then-redirect idiom as `error_redirect`, for a *successful*
    outcome: a separate `notice` query param so the page can render it with
    `.flash-ok` instead of `.error` (Finding 4 -- a successful revision
    approval previously had nowhere to go but the `error` channel, so it
    rendered in red error styling)."""
    return RedirectResponse(f"{target}?notice={quote(message)}", status_code=303)


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request) -> HTMLResponse:
    c = request.app.state.holder.get()

    # Computed first, before either the broker calls or the strategy/quote
    # loop below -- this line's entire purpose is answering "is my system
    # wedged?", so it must never wait behind up to 2 * BROKER_TIMEOUT_SECONDS
    # of broker calls plus the quote budget below to reach the page.
    sentinel_status = sentinel_heartbeat_status(
        c.app_state.get(SENTINEL_HEARTBEAT_KEY), c.settings.sentinel_interval_minutes,
        market_open_raw=c.app_state.get(SENTINEL_MARKET_OPEN_KEY))

    # Same is_market_hours the scheduler/sentinel gate their pass on (see
    # allpath_trade/scheduler.py) -- reused rather than reimplemented so the
    # pill can never disagree with what actually decided whether today's
    # sentinel pass ran. Like that function, this is weekday+hours only, no
    # holiday calendar (see docs/TODO.md): the pill will read "open" on a
    # market holiday, same limitation the heartbeat line already carries.
    market_open = is_market_hours()

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

    # One shared QUOTES_BUDGET_SECONDS deadline for the whole loop, not a
    # fresh BROKER_TIMEOUT_SECONDS allowance per strategy -- once it's spent,
    # remaining tickers render as "—" instead of each queuing another call
    # against the (already possibly saturated, see _broker_pool's comment
    # above) pool. A call already in flight when the deadline lands still
    # runs to completion in the background; there's no way to cancel a
    # thread mid-call, only to stop waiting on it (that's _with_timeout's
    # job, per-call).
    quote_deadline = time.monotonic() + QUOTES_BUDGET_SECONDS
    strategy_cards = []
    for s in strategies:
        quote = (_cached_quote(c.data, s.position.ticker)
                 if time.monotonic() < quote_deadline else None)
        strategy_cards.append(summarize_strategy(
            s, positions_by_ticker.get(s.position.ticker), quote,
            equity=equity, has_pending=s.id in pending_strategy_ids))

    return templates.TemplateResponse(request, "dashboard.html", {
        "page": "dashboard", "account": account, "positions": positions,
        "broker_error": broker_error, "strategy_cards": strategy_cards,
        "strategy_errors": errors, "trades": c.journal.recent(limit=8),
        "sentinel_status": sentinel_status, "market_open": market_open,
        **nav_context(c)})
