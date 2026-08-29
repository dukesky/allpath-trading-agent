from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from decimal import Decimal

from allpath_trade.scheduler import ET, ts_to_et_date

# Same masking convention as telegram.py's TelegramAPI._scrub -- the
# publish token rides in an Authorization header rather than a URL path, so
# it is less likely to land in a transport exception's message than
# Telegram's path-embedded bot token is, but "less likely" is not "never":
# a proxy error, a redirected-URL error, or a future header-echoing bug
# could still put it there, and the token must never reach stderr either
# way.
_TOKEN_MASK = "***"

# The reflection report body can run to tens of thousands of characters for
# a verbose nightly session; the public journal page only needs enough to
# show the day's write-up, not an unbounded blob. Chosen to match the spec
# exactly (20000 chars).
_MAX_REFLECTION_BODY_CHARS = 20_000

# How many of the account's most recent journal rows to scan when filtering
# down to one ET calendar day's trades. TradeJournal has no dedicated
# date-range query (see store/journal.py); a fixed, generous scan window is
# simpler than adding one, and comfortably covers even a very active
# trading day.
_TRADE_SCAN_LIMIT = 2000

# POST timeout for the publish call -- generous for a small JSON payload to
# a presumably-fast public endpoint, but bounded so a stalled destination
# can't hang the whole nightly chain behind it.
_PUBLISH_TIMEOUT_SECONDS = 15


def _scrub(text: str, token: str) -> str:
    """Mask every occurrence of `token` in `text`. A no-op on an empty
    token, matching telegram.py's `TelegramAPI._scrub`."""
    if not token:
        return text
    return text.replace(token, _TOKEN_MASK)


def _trade_row_to_dict(row) -> dict:
    """One journal row -> the digest's trade shape. `qty`/`notional`/
    `filled_qty`/`filled_avg_price` are already TEXT columns in the trades
    table (TradeJournal.record stores `str(...)` before insert) -- they come
    back from sqlite3.Row as plain strings (or None) already, so no Decimal
    conversion is needed here."""
    return {
        "ticker": row["ticker"],
        "side": row["side"],
        "qty": row["qty"],
        "notional": row["notional"],
        "status": row["status"],
        "submitted_ts": row["ts"],
        "filled_at": row["filled_at"],
        "filled_avg_price": row["filled_avg_price"],
        "filled_qty": row["filled_qty"],
        "reason": row["reason"],
    }


def _day_change(broker, date_et: str, current_equity: Decimal) -> tuple[str | None, float | None]:
    """(day_change, day_change_pct) vs the most recent equity-history point
    dated strictly before `date_et` in ET -- i.e. the prior trading day's
    close, whether or not `get_equity_history` also happens to already carry
    a same-day point.

    Never raises: a broker whose history call fails (a dead broker, a
    yfinance hiccup) or returns nothing to compare against degrades to
    "unknown" (None, None) rather than taking the whole digest down over an
    optional field -- same defensive posture as `_llm_cost_line` in
    scheduler.py."""
    try:
        history = broker.get_equity_history(7)
    except Exception:  # noqa: BLE001 — an optional field must not cancel the digest
        return None, None
    prior = [equity for ts, equity in history
             if ts.astimezone(ET).date().isoformat() < date_et]
    if not prior:
        return None, None
    previous = prior[-1]
    if not previous:
        return None, None
    change = current_equity - previous
    pct = float(change / previous * 100)
    return str(change), round(pct, 2)


def build_daily_digest(components, date_et: str) -> dict:
    """Assemble one ET trading day's public-journal digest for the PAPER
    account (`components` here is the top-level `Components` object, whose
    `.broker`/`.journal`/`.reports`/`.queue` are the legacy aliases for
    `accounts["paper"]` -- see app.py's `Components` docstring; the public
    journal shows the real paper account, not the shadow ledger).

    Pure assembly -- no network call. Money fields are rendered as strings
    (Decimal -> str), never float, so the JSON payload never silently loses
    cent-level precision. Deliberately carries nothing beyond what's listed
    below: no strategy YAML, no memory content, no conversation text past
    the reflection report itself."""
    account = components.broker.get_account()
    equity = account.equity
    day_change, day_change_pct = _day_change(components.broker, date_et, equity)

    trades = [
        _trade_row_to_dict(row)
        for row in components.journal.recent(limit=_TRADE_SCAN_LIMIT)
        if ts_to_et_date(row["ts"]) == date_et
    ]

    report = components.reports.get(date_et)
    reflection_summary = report["summary"] if report is not None else ""
    reflection_body = (report["body"][:_MAX_REFLECTION_BODY_CHARS]
                       if report is not None else "")

    try:
        pending_proposals = sum(
            1 for row in components.queue.list("pending")
            if row["kind"] == "strategy_revision")
    except Exception:  # noqa: BLE001 — an optional field must not cancel the digest
        pending_proposals = 0

    return {
        "date": date_et,
        "equity": str(equity),
        "day_change": day_change,
        "day_change_pct": day_change_pct,
        "trades": trades,
        "reflection_summary": reflection_summary,
        "reflection_body": reflection_body,
        "pending_proposals": pending_proposals,
    }


def publish_digest(url: str, token: str, digest: dict) -> bool:
    """POST `digest` as JSON to `url`, authenticated with `Authorization:
    Bearer <token>`. Stdlib `urllib` only, same one-call-HTTP precedent as
    `notify/ntfy.py`'s `NtfyNotifier.send` -- no new runtime dependency for
    a single POST.

    Never raises: any failure (a bad URL, a network error, a non-2xx
    response) is reported as a single scrubbed stderr line and `False`.
    `token` is masked out of that line via `_scrub` regardless of where in
    the failure text it might appear (a transport exception's message, an
    echoed request URL) -- same belt-and-suspenders posture as telegram.py's
    `TelegramAPI._scrub`, which is applied unconditionally rather than only
    to the paths that are known today to embed the token.

    Returns True only for a genuine 2xx response."""
    try:
        body = json.dumps(digest).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_PUBLISH_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
    except Exception as exc:  # noqa: BLE001 — publish must never crash the caller
        print(f"[publish] failed: {_scrub(str(exc), token)}", file=sys.stderr)
        return False
    if 200 <= status < 300:
        return True
    print(f"[publish] failed: {_scrub(f'HTTP {status}', token)}", file=sys.stderr)
    return False
