from __future__ import annotations

import concurrent.futures
import re
import time
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from allpath_trade.broker.base import Broker, Position
from allpath_trade.broker.unconfigured import UnconfiguredBroker
from allpath_trade.data.base import DataSource, Quote
from allpath_trade.scheduler import is_market_hours, today_et_date
from allpath_trade.store.accounts import ACCOUNTS
from allpath_trade.store.app_state import (
    SENTINEL_HEARTBEAT_KEY,
    SENTINEL_LAST_OK_KEY,
    SENTINEL_MARKET_OPEN_KEY,
)
from allpath_trade.strategy.model import RuleState, RuleType, StrategyDoc
from allpath_trade.web.account_ctx import bundle, bundle_for, current_account
from allpath_trade.web.charts import equity_since_caption, equity_svg, signed_money, signed_pct
from allpath_trade.web.deps import components
from allpath_trade.web.setup_status import setup_missing
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


def nav_context(request: Request) -> dict:
    """Nav-chrome context shared by every page: the current account's own
    pending count (drives the badge on the "Pending" nav link, as before
    shadow-dual-active), PLUS the account switcher's own state.

    Pending-badge decision (shadow-dual-active T5): the badge counts ONLY
    the CURRENT account's pending reviews, not both accounts' combined --
    switching accounts is cheap (one click) and the badge is meant to answer
    "does the account I'm LOOKING AT need me", not "does anything anywhere
    need me" (which would make the badge number disagree with what's
    actually listed on the /reviews page the badge links to). The OTHER
    account not being silently invisible is handled separately: the
    switcher chip for the account you're NOT on gets a small dot
    (`other_account_pending`) when IT has pending rows, so nothing is ever
    truly hidden -- just not double-counted into one badge."""
    c = components(request)
    current = current_account(request)
    pending_count = len(bundle_for(c, current).queue.list())
    other = next(a for a in ACCOUNTS if a != current)
    other_pending = bool(bundle_for(c, other).queue.list())
    # setup-wizard T2: the banner. Independent of the dismissal flag on
    # purpose -- the redirect gate (web/auth.py) is what dismissal turns
    # off, and in practice this only ever renders for someone who DID
    # dismiss, since anyone who hasn't never reaches a page carrying it.
    # Suppressed on the wizard's own pages, where a banner pointing at the
    # page you are already on is just noise.
    missing = setup_missing(c.settings)
    return {
        "pending_count": pending_count,
        "current_account": current,
        "other_account": other,
        "other_account_pending": other_pending,
        "setup_missing": missing,
        "setup_banner": bool(missing) and not _is_setup_path(request.url.path),
    }


def _is_setup_path(path: str) -> bool:
    return path == "/setup" or path.startswith("/setup/")


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


# Public alias -- other route modules (web/routes/strategies.py's card quote
# lookup) need this same cached lookup and must not reach across module
# boundaries into a name prefixed `_private`.
cached_quote = _cached_quote


def _last_trading_day_cutoff(now: datetime) -> datetime:
    """The instant before which a shadow position's `last_price_ts`
    (broker/shadow.py's `_valuation_price` docstring: "staleness surfaced
    to the user via last_price_ts -- Task 7 renders it") counts as stale
    for the dashboard's "price as of" column -- "1 trading day ago", with
    the same weekday-only calendar `is_market_hours`/`_is_after_close`
    already use (no holiday awareness -- see docs/TODO.md, same documented
    limitation). Monday's cutoff reaches back to Friday (the previous
    trading day), not Sunday, so a price set at Friday's close is still
    "yesterday's trading day" Monday morning rather than flagged stale by a
    plain 24h window."""
    days_back = 3 if now.weekday() == 0 else 1  # Monday -> back to Friday
    return now - timedelta(days=days_back)


def _shadow_price_staleness(b, positions: list[Position], data: DataSource,
                            now: datetime) -> dict[str, str | None]:
    """`{ticker: last_price_ts}` for every shadow position whose displayed
    valuation the dashboard should flag with a "price as of" note --
    shadow-dual-active T7, spec §⑧: "行情失败:最后已知价 + 标注". Empty (no
    flags at all) on the paper account -- Alpaca's own live feed has no
    analogous "last known, possibly stale" concept for this app to surface.

    A ticker is flagged when EITHER of spec §⑧'s two conditions holds:
    `last_price_ts` is missing or older than one trading day
    (`_last_trading_day_cutoff`), OR a fresh quote attempt for it fails
    right now (`_cached_quote` -- the exact same cached lookup the
    strategy cards already use, so this costs no extra network calls on a
    warm cache). Reads `shadow_positions.last_price_ts` directly via raw
    SQL (mirrors `web/routes/settings.py`'s own `_shadow_ledger_summary`)
    because `Broker.get_positions()`'s `Position` model -- shared with
    paper/Alpaca -- has no timestamp field to carry this shadow-only
    concept through."""
    if b.account != "shadow" or not positions:
        return {}
    rows = b.conn.execute("SELECT ticker, last_price_ts FROM shadow_positions")
    ts_by_ticker = {row["ticker"]: row["last_price_ts"] for row in rows}
    cutoff = _last_trading_day_cutoff(now)
    stale: dict[str, str | None] = {}
    for p in positions:
        ts = ts_by_ticker.get(p.ticker)
        quote_failed = _cached_quote(data, p.ticker) is None
        ts_stale = ts is None
        if not ts_stale:
            parsed = datetime.fromisoformat(ts)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            ts_stale = parsed.astimezone(UTC) < cutoff
        if quote_failed or ts_stale:
            stale[p.ticker] = ts
    return stale


# Dashboard equity-chart range tabs. Values are the `?range=` query string;
# `_RANGE_DAYS` maps each to the lookback window passed to
# Broker.get_equity_history -- everything except "ytd" is a fixed count,
# "ytd" is computed per-request in `_range_days` below since the right day
# count changes every calendar day.
_RANGE_LABELS = {"1w": "Week", "1m": "Month", "ytd": "YTD", "1y": "Year"}
_DEFAULT_RANGE = "1m"
_RANGE_DAYS = {"1w": 7, "1m": 30, "1y": 365}


def _range_days(range_key: str, now: datetime) -> int:
    if range_key == "ytd":
        # Anchored to `today_et_date` -- the same ET calendar "today" the
        # reports page's Today/This week/This month chips already use --
        # not `now`'s own (UTC) calendar date. Late evening ET is already
        # past midnight UTC for several hours a day; anchoring to UTC would
        # occasionally compute YTD as "since yesterday" while ET, and every
        # other "what day is it" computation in this app, still considers it
        # the previous calendar day.
        today = date.fromisoformat(today_et_date(now))
        jan1 = date(today.year, 1, 1)
        return max((today - jan1).days, 1)
    return _RANGE_DAYS.get(range_key, _RANGE_DAYS[_DEFAULT_RANGE])


_EQUITY_HISTORY_CACHE_TTL_SECONDS = 300  # 5 min -- same reasoning as the quote cache
# A failed (or empty) lookup gets a much shorter TTL than a working one -- a
# quote failure degrades to a quiet "—" that blends into a page full of other
# text, but a failed equity history degrades to a large, obvious empty chart
# card; holding that up for the full 5 minutes is far more conspicuous than
# the quote cache's equivalent failure, so a stale broker hiccup shouldn't
# get to keep the chart blank nearly as long.
_EQUITY_HISTORY_FAILURE_TTL_SECONDS = 30
# Module-level, keyed by (broker.name, range) -- same "survive across
# requests, not just within one render" rationale as `_quote_cache` above.
#
# shadow-dual-active T4 review (Important 4): this used to be keyed by
# `range_key` ALONE on the assumption that "a single-process app talks to
# one broker" -- that stopped being true the moment paper and shadow
# started sharing this one process. Whichever account's dashboard render
# happened to populate a given range's slot first would silently serve
# ITS equity curve to the other account's page for the rest of the TTL
# (a paper-curve-under-shadow-view leak, or vice versa) until the cache
# happened to expire. `broker.name` ("alpaca" for paper, "shadow" for the
# ledger) is a sufficient second key -- each account's own broker instance
# never changes identity across requests within one process.
_equity_history_cache: dict[tuple[str, str], tuple[float, list]] = {}


def _cached_equity_history(broker: Broker, range_key: str, days: int) -> list:
    now = time.monotonic()
    cache_key = (broker.name, range_key)
    cached = _equity_history_cache.get(cache_key)
    if cached is not None:
        cached_at, cached_history = cached
        # An empty cached result reads as "the last attempt failed (or a
        # genuinely empty history)" either way -- both get the short TTL, so
        # a real failure retries promptly instead of hiding behind the long
        # success TTL meant for a working chart.
        ttl = (_EQUITY_HISTORY_CACHE_TTL_SECONDS if cached_history
               else _EQUITY_HISTORY_FAILURE_TTL_SECONDS)
        if now - cached_at < ttl:
            return cached_history
    try:
        history = _with_timeout(lambda: broker.get_equity_history(days))
    except Exception:  # noqa: BLE001 — a history failure must render the placeholder, never an error page
        history = []
    _equity_history_cache[cache_key] = (now, history)
    return history


def equity_period_summary(history: list, account_equity) -> dict:
    """Pure function -- the "current equity big + period change" line above
    the chart. `account_equity` (the broker's real-time balance, already
    fetched for the metrics cards) is used as "current", not the equity
    history's own last point, since Alpaca's portfolio-history series is
    end-of-day while the account call is live; the period baseline is still
    the history's first point in range. Fewer than two history points --
    empty (no history, or a broker/timeout failure already degraded to
    `[]`), or exactly one point with nothing else to compare it against --
    means there is nothing to show a period change against, so this renders
    nothing. `equity_svg` also draws no line below two points (a muted
    placeholder instead), so this keeps the headline and the chart agreeing
    about when there's "enough" history to say anything at all -- a
    one-point account otherwise showed a real change figure sitting right
    above what is, visually, an empty chart."""
    if len(history) < 2 or account_equity is None:
        return {"current": None, "change": None, "change_pct": None}
    first_equity = history[0][1]
    change = account_equity - first_equity
    change_pct = float(change / first_equity * 100) if first_equity else None
    return {"current": account_equity, "change": change, "change_pct": change_pct}


def order_price_context(data: DataSource, ticker: str, snapshot: dict | None,
                        intent: dict | None) -> dict:
    """Price context for a pending order-kind review: the trigger price
    (the sentinel's own snapshot, when this row has one -- chat-sourced
    rows don't), a freshly (cached) current price with day-change
    direction, the deviation between the two, and an estimated share count
    for a notional-sized intent. Shared by the reviews page's pending
    cards (web/routes/reviews.py) and the standalone approve-link
    confirmation page (web/routes/approve.py) so both surfaces agree on
    exactly the same numbers -- same shape as `summarize_strategy` above:
    a quote failure degrades every current-price-derived field to `None`
    rather than raising, so callers can render "omit gracefully" cards
    either way."""
    trigger_price = None
    if snapshot and snapshot.get("price"):
        try:
            trigger_price = Decimal(str(snapshot["price"]))
        except (InvalidOperation, TypeError, ValueError):
            trigger_price = None

    quote = _cached_quote(data, ticker) if ticker else None
    current_price = quote.price if quote is not None else None

    day_change_pct = None
    price_class = ""
    if quote is not None and quote.price is not None and quote.previous_close:
        day_change_pct = float((quote.price - quote.previous_close) / quote.previous_close * 100)
        if day_change_pct > 0:
            price_class = "up"
        elif day_change_pct < 0:
            price_class = "down"

    deviation_pct = None
    if trigger_price and current_price and trigger_price != 0:
        deviation_pct = float((current_price - trigger_price) / trigger_price * 100)

    est_shares = None
    if (intent and not intent.get("qty") and intent.get("notional")
            and current_price):
        try:
            est_shares = Decimal(str(intent["notional"])) / current_price
        except (InvalidOperation, ZeroDivisionError, TypeError, ValueError):
            est_shares = None

    return {
        "trigger_price": trigger_price, "current_price": current_price,
        "day_change_pct": day_change_pct, "price_class": price_class,
        "deviation_pct": deviation_pct, "est_shares": est_shares,
        "market_open": is_market_hours(),
    }


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
                              last_ok_raw: str | None = None,
                              now: datetime | None = None,
                              broker_unconfigured: bool = False) -> dict:
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
    when it landed.

    `last_ok_raw` is SENTINEL_LAST_OK_KEY's per-account value (I7), written
    only after `sentinel.run_once()` actually returned. The heartbeat above
    is written BEFORE the check runs -- deliberately, it is the "scheduler
    is alive" signal -- which meant a sentinel raising on every single tick
    (dead data source, expired broker credentials) still rendered a
    perfectly reassuring "last check 0m ago" indefinitely. Two independent
    warnings now: the tick itself going stale (a dead daemon), and the tick
    running while the CHECK keeps failing. The second gets one interval of
    slack, because a single failed pass between two ticks is ordinary
    transient noise, and is suppressed entirely while the market is closed,
    where a lagging last-ok is the expected state rather than a fault
    (warning every weekend would just train the operator to ignore it).
    Missing or unparseable `last_ok_raw` reads as "no successful check
    recorded" and warns -- unlike `market_open_raw`, absence here is not
    ambiguous: an account whose sentinel has ever completed a pass during
    market hours has this key.

    `broker_unconfigured` (setup-wizard T1) short-circuits everything
    above. With no Alpaca keys the scheduler still ticks and still writes
    the heartbeat -- it is genuinely alive -- so every branch below would
    render some flavour of "last check Nm ago", which is true and useless:
    nothing is being monitored at all until setup is finished, and that is
    the only fact worth a line here. It warns unconditionally for the same
    reason, including outside market hours and including when the heartbeat
    is missing entirely: unlike a lagging last-ok on a weekend, this state
    never resolves itself with time."""
    if broker_unconfigured:
        return {"text": "Sentinel: paper broker not configured — finish setup",
                "warn": True}
    if raw is None:
        return {"text": "Sentinel: never ran (daemon not running?)", "warn": False}
    try:
        last = _parse_heartbeat_ts(raw)
    except (TypeError, ValueError):
        return {"text": "Sentinel: never ran (daemon not running?)", "warn": False}
    now = now or _utcnow()
    elapsed = now - last
    minutes = max(0, int(elapsed.total_seconds() // 60))
    warn = minutes > 2 * interval_minutes
    if market_open_raw == "false":
        text = f"Sentinel: monitoring paused (market closed) · last tick {minutes}m ago"
        return {"text": text, "warn": warn}

    last_ok = None
    if last_ok_raw is not None:
        try:
            last_ok = _parse_heartbeat_ts(last_ok_raw)
        except (TypeError, ValueError):
            last_ok = None
    if last_ok is None:
        return {"text": (f"Sentinel: last check {minutes}m ago"
                        " · no successful check recorded"), "warn": True}
    ok_minutes = max(0, int((now - last_ok).total_seconds() // 60))
    if ok_minutes - minutes > interval_minutes:
        return {"text": (f"Sentinel: last check {minutes}m ago"
                        f" · last successful {ok_minutes}m ago"), "warn": True}
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
def dashboard(request: Request,
              range_key: str = Query(_DEFAULT_RANGE, alias="range")) -> HTMLResponse:
    # Renamed from the query param's own name (`range`) -- that shadows the
    # `range` builtin for the whole function body; `alias="range"` keeps the
    # `?range=` URL contract unchanged while the Python-side name doesn't.
    c = components(request)
    # shadow-dual-active T5: `b` is the CURRENT account's bundle (broker,
    # strategies, queue, journal, sentinel heartbeat) -- for paper (the
    # default, cookie-less view) this is `c` itself, so nothing about the
    # pre-existing single-account dashboard changes. `c` stays in scope for
    # genuinely shared reads: settings, app_state, the data source.
    b = bundle(request)

    # Whitelist, not a 400 -- an unrecognized `?range=` (a stale bookmark, a
    # hand-edited URL) just falls back to the default tab rather than
    # breaking the page.
    if range_key not in _RANGE_LABELS:
        range_key = _DEFAULT_RANGE

    # Computed first, before either the broker calls or the strategy/quote
    # loop below -- this line's entire purpose is answering "is my system
    # wedged?", so it must never wait behind up to 2 * BROKER_TIMEOUT_SECONDS
    # of broker calls plus the quote budget below to reach the page.
    #
    # shadow-dual-active T5: reads `sentinel_last_pass:{account}` -- the
    # per-account heartbeat key scheduler.py's `_run_sentinel_pass` writes
    # (T4) -- not the bare legacy key, so the dashboard's heartbeat line
    # always reflects the account currently being viewed, not always
    # paper's own pass regardless of which account is on screen.
    # setup-wizard T1/T4: shared by the heartbeat line below AND the
    # broker-failure slot's guidance card further down -- both need to
    # single out "not configured yet" from every other broker failure, and
    # must agree on the same check rather than risk drifting apart.
    broker_unconfigured = isinstance(b.broker, UnconfiguredBroker)
    sentinel_status = sentinel_heartbeat_status(
        c.app_state.get(f"{SENTINEL_HEARTBEAT_KEY}:{b.account}"),
        c.settings.sentinel_interval_minutes,
        market_open_raw=c.app_state.get(SENTINEL_MARKET_OPEN_KEY),
        # I7: the per-account SUCCESS heartbeat, read alongside the plain
        # one -- the heartbeat proves the scheduler ticked, this proves the
        # tick actually evaluated anything.
        last_ok_raw=c.app_state.get(f"{SENTINEL_LAST_OK_KEY}:{b.account}"),
        # setup-wizard T1: the scheduler skips this account's pass entirely
        # while the keys are missing (scheduler.py's `_run_sentinel_pass`),
        # so the heartbeat below it must say so rather than reporting on
        # ticks that deliberately checked nothing.
        broker_unconfigured=broker_unconfigured)

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
        account = _with_timeout(b.broker.get_account)
        positions = _with_timeout(b.broker.get_positions)
    except concurrent.futures.TimeoutError:
        broker_error = f"Broker unavailable: timed out after {BROKER_TIMEOUT_SECONDS}s"
    except Exception as exc:  # noqa: BLE001 — a broker outage must not blank the page
        broker_error = f"Broker unavailable: {exc}"

    days = _range_days(range_key, _utcnow())
    equity_history = _cached_equity_history(b.broker, range_key, days)
    equity_summary = equity_period_summary(
        equity_history, account.equity if account is not None else None)
    # None (not a plain bool) when there's no headline to show at all
    # (equity_summary["change"] is None) -- passing a forced False into
    # `equity_svg` in that case would paint the chart "down" with no
    # headline sign to actually justify it; None instead falls through to
    # equity_svg's own last-vs-first fallback. When there IS a headline,
    # this is the one comparison that then decides both the headline's sign
    # AND the chart line's colour (Important 3), so they can never disagree.
    equity_up_for_chart = (None if equity_summary["change"] is None
                           else equity_summary["change"] >= 0)

    # shadow-dual-active T7: `{}` on paper, always -- see
    # `_shadow_price_staleness`'s own docstring for why only a shadow
    # position's valuation carries this "last known, possibly stale" idea
    # at all.
    price_as_of = _shadow_price_staleness(b, positions, c.data, _utcnow())

    errors: list[str] = []
    strategies = b.strategies.load_all(status=None, errors=errors)
    positions_by_ticker = {p.ticker: p for p in positions}
    pending_strategy_ids = {row["strategy_id"] for row in b.queue.list("pending")}
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
        "price_as_of": price_as_of,
        "broker_error": broker_error, "broker_unconfigured": broker_unconfigured,
        "strategy_cards": strategy_cards,
        "strategy_errors": errors, "trades": b.journal.recent(limit=8),
        "sentinel_status": sentinel_status, "market_open": market_open,
        "range": range_key, "range_labels": _RANGE_LABELS,
        "equity_svg_markup": equity_svg(equity_history, up=equity_up_for_chart),
        "equity_since": equity_since_caption(equity_history),
        "equity_current": equity_summary["current"],
        "equity_change_str": (signed_money(equity_summary["change"])
                              if equity_summary["change"] is not None else None),
        "equity_change_pct_str": (signed_pct(equity_summary["change_pct"])
                                  if equity_summary["change_pct"] is not None else None),
        "equity_up": bool(equity_summary["change"] is not None and equity_summary["change"] >= 0),
        **nav_context(request)})
