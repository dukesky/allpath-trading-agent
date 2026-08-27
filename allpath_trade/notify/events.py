from __future__ import annotations

import sys
from decimal import Decimal

from allpath_trade.store.accounts import ACCOUNTS

# Approve-by-link (Part A) is opt-in via Settings -> Access (web_base_url):
# unset (the default), a notification never carries a link at all, same as
# before that feature existed. When it IS set, `review_queued` below may
# add one -- but even then it is never your dashboard access token: it's a
# single-use, 24-hour token scoped to approving/rejecting this one item,
# dead the moment it's used or expires. This footer has to stay true in
# both cases, since it's appended to every notification regardless of
# whether that particular one carries a link.
FOOTER = ("\n\nOpen the AllPath Trade dashboard to act on this. "
          "This message never carries your dashboard access token. If "
          "you've opted into approval links under Settings, an item "
          "waiting for you may also include a one-time link scoped to "
          "just that item, valid for 24 hours and dead as soon as it's used.")


def _prefix(account: str) -> str:
    """The `[Paper] `/`[Shadow] ` subject-line prefix for `account`
    (shadow-dual-active T7, spec §⑤: "所有事件...主题加前缀
    `[Paper]`/`[Shadow]`") -- the ONE place every builder in this module
    gets it from, so the bracket/capitalization shape can never drift
    between events.

    `account` not being one of `store.accounts.ACCOUNTS` (a caller bug --
    every real call site threads a validated account through) must NOT
    crash notification delivery, which is the whole point of this
    function existing rather than every builder inlining an f-string: it
    degrades to `""` (no prefix -- the subject reads exactly as it did
    before account-prefixing existed) and logs to stderr so the bug is
    still visible somewhere, same "never break the send" contract every
    other notify-layer failure in this codebase already follows."""
    if account not in ACCOUNTS:
        print(f"[notify] unknown account {account!r}; sending unprefixed",
             file=sys.stderr)
        return ""
    return f"[{account.capitalize()}] "


def approve_link(base_url: str, review_id: object) -> str:
    """Builds the Phase 6 one-time approve-by-link URL for a review that was
    just queued -- shared by every queueing path (sentinel.py's rule
    triggers, agent/action_tools.py's chat strategy drafts) so the URL shape
    lives in exactly one place rather than being re-derived per caller.

    Returns `""` (no link) unless BOTH the operator opted in (`base_url`
    set, Settings -> Access) AND the row actually carries a live token --
    `getattr(review_id, "token", None)` reads `ReviewHandle.token`
    (store/reviews.py) without requiring the caller's `review_id` to be
    that exact type; a plain `int` (no `.token` attribute) degrades to no
    link, same as a `ReviewHandle` built without one."""
    token = getattr(review_id, "token", None)
    if base_url and token:
        return f"{base_url}/a/{int(review_id)}?k={token}"
    return ""


def rule_triggered(*, account: str, strategy_id: str, rule_id: str, ticker: str,
                   condition: str, disposition: str) -> tuple[str, str]:
    subject = f"{_prefix(account)}[AllPath] {ticker}: rule {rule_id} triggered"
    body = (f"Strategy {strategy_id}, rule {rule_id} triggered on {ticker}.\n"
            f"Condition: {condition}\n"
            f"Disposition: {disposition}." + FOOTER)
    return subject, body


def order_result(*, account: str, ticker: str, side: str, submitted: bool,
                 detail: str, filled_qty: Decimal | None = None,
                 filled_avg_price: Decimal | None = None) -> tuple[str, str]:
    """`filled_qty`/`filled_avg_price` (from the `Order` the executor
    returned, when there is one) are ONLY used for the shadow wording
    below -- paper's own body is unchanged by this task and never reads
    them.

    shadow-dual-active T7 (spec §⑤: "shadow 的订单文案 = 建议语气 +
    'place this order in your brokerage now'"): a shadow-account order
    that actually got recorded (`submitted=True` -- the ledger's own risk
    gate approved it, same field paper's executor sets) is NOT an order
    routed anywhere -- `ShadowLedger` has no real brokerage behind it, see
    broker/shadow.py -- so calling it "submitted" without qualification
    would be a lie by omission. This applies to BOTH ways a shadow order
    reaches here: a hard-rule auto-execution (`Sentinel._dispatch`'s
    AUTO+HARD branch) and a soft rule the user (or the review agent, on
    their behalf) approved (`Sentinel._agent_review`'s AUTO+SOFT
    autonomous branch or a human `ReviewQueue.approve()`) -- both paths
    funnel through the same `_notify_order`, so one wording change here
    covers both. A rejected/errored shadow order (`submitted=False`)
    never reached the ledger at all, so it keeps the same generic
    "not submitted" wording paper gets -- there is nothing to go place
    anywhere.

    C3: the SUBJECT carries the same honesty as the body. It is also the
    ntfy push title -- the one string a phone lockscreen shows -- so a
    subject reading "order submitted" over a body reading "place it
    yourself" would still tell the lie on the surface most readers see
    first, and only ever see."""
    outcome = "submitted" if submitted else "not submitted"
    shadow_order = account == "shadow" and submitted
    headline = "order recorded — place it yourself" if shadow_order else f"order {outcome}"
    subject = f"{_prefix(account)}[AllPath] {ticker}: {headline}"
    if shadow_order:
        fill = ""
        if filled_qty is not None and filled_avg_price is not None:
            fill = f": {side.upper()} {filled_qty} {ticker} @ ~${filled_avg_price:,.2f}"
        body = (f"A {side} order for {ticker} was recorded in your shadow "
                f"ledger — place this order in your brokerage now{fill}.\n"
                f"{detail}") + FOOTER
    else:
        body = f"A {side} order for {ticker} was {outcome}.\n{detail}" + FOOTER
    return subject, body


def drawdown_halt(*, account: str, peak: Decimal, equity: Decimal,
                  drawdown: Decimal, demoted: list[str]) -> tuple[str, str]:
    """The drawdown circuit breaker's own alert (Task 7) -- always sent
    through `self.notifier.send` directly plus `push_telegram_receipt`, NOT
    the per-strategy `_send`/`notify_email` gate every other sentinel event
    goes through: an account-level halt is not a per-strategy notification
    preference, so it must reach the operator even when every strategy on
    file happens to have `notify_email: false`."""
    subject = (f"{_prefix(account)}[AllPath] TRADING HALTED: "
               f"{drawdown:.1%} drawdown")
    names = ", ".join(demoted) if demoted else "none were set to auto"
    body = (f"Equity ${equity:,.2f} is {drawdown:.1%} below its peak "
            f"${peak:,.2f}.\n"
            f"The drawdown circuit breaker tripped. Auto strategies demoted "
            f"to confirm: {names}.\n"
            "No further orders will execute without your approval.\n"
            "To resume: review the account, restore strategies to auto "
            "deliberately, then run `allpath-trade breaker reset`." + FOOTER)
    return subject, body


def review_queued(*, account: str, review_id: int, ticker: str, action: str,
                  strategy_id: str, recommendation: str = "",
                  trigger_price: str = "", est_shares: str = "",
                  approve_url: str = "", kind: str = "order") -> tuple[str, str]:
    """`trigger_price`/`est_shares` are the price context available at the
    instant this item was queued (Part B) -- the same sample the rule
    triggered on, not a second, separately-fetched "live" quote (the
    sentinel has no cheap way to requote here, and a second number this
    close in time to the first would only look like independent
    confirmation it isn't). `approve_url`, when non-empty, is the one place
    a notification body ever carries a link -- see FOOTER's docstring for
    why that's still opt-in and safe to describe truthfully there.

    M2: `ticker` is `""` for a ticker-less shadow ledger edit (set_cash,
    import, reset -- agent/shadow_tools.py, web/routes/settings.py's CSV/
    reset routes), which used to leave a bare "[AllPath] : waiting for
    your approval" subject and a "Proposed: ... on " body line with
    nothing after "on". "Ledger" stands in as the subject noun for these
    -- honest (it IS the shadow ledger being changed) without inventing a
    ticker that was never there.

    shadow-dual-active T7: `kind` (default `"order"`, matching every
    pre-existing caller -- sentinel.py's soft-rule/confirm triggers and
    web/order_sink.py's chat order proposals both queue an actual order)
    is the ONE thing that decides whether the shadow "you'll place it
    yourself" clause below applies. It does NOT apply to a
    `kind="strategy_revision"` draft (agent/action_tools.py) -- approving
    that writes a strategy file, not an order -- nor to
    `kind="shadow_edit"` (agent/shadow_tools.py, web/routes/settings.py's
    CSV/reset routes) -- that message is already about the ledger edit
    itself, and a second "you'll place it yourself" line there would be
    actively confusing (there is nothing to place; the edit IS the whole
    action). Spec §⑤: "shadow 的订单文案 = 建议语气 + ...", applied to the
    queued-review case as "if approved, you'll place it yourself"."""
    subject_noun = ticker or "Ledger"
    subject = f"{_prefix(account)}[AllPath] {subject_noun}: waiting for your approval"
    proposed = f"Proposed: {action} on {ticker}" if ticker else f"Proposed: {action}"
    lines = [f"Item #{review_id} is waiting for you.", proposed]
    if strategy_id:
        lines.append(f"Strategy: {strategy_id}")
    if trigger_price:
        lines.append(f"Price at trigger: {trigger_price}")
    if est_shares:
        lines.append(f"Est. size: ~{est_shares} shares at that price")
    if recommendation:
        lines.append(f"The agent recommends: {recommendation}")
    if account == "shadow" and kind == "order":
        lines.append("If approved, you'll place it yourself — approving here "
                     "only records it in your shadow ledger.")
    body = "\n".join(lines)
    if approve_url:
        body += f"\nReview & approve: {approve_url}"
    return subject, body + FOOTER


def daily_digest(*, account: str, triggers: int, trades: int, pending: int,
                 llm_cost: str = "") -> tuple[str, str]:
    """`llm_cost`, when non-empty, is one extra line naming today's
    estimated LLM spend (store/llm_usage.py + llm/prices.py) -- the caller
    (scheduler.py's `_send_daily_digest`) only ever passes a non-empty
    string when there was actually usage to report, so this stays the same
    silent no-op it always was on a day with no LLM calls at all.

    shadow-dual-active T7: one digest per account now (scheduler.py's
    `_send_daily_digest` calls this once per entry in `ACCOUNTS`) -- the
    body names which account it's about, and for shadow says "recorded"
    rather than "trade(s)", matching order_result's own "recorded, not
    executed" honesty. `account not in ACCOUNTS` (same degrade as
    `_prefix`) falls back to the original unlabeled "Today: ..." wording
    rather than guessing."""
    subject = f"{_prefix(account)}[AllPath] Daily summary"
    if account == "shadow":
        body = (f"Shadow account today: {triggers} rule trigger(s), "
                f"{trades} order(s) recorded, "
                f"{pending} item(s) still waiting for your approval.")
    elif account == "paper":
        body = (f"Paper account today: {triggers} rule trigger(s), "
                f"{trades} trade(s), "
                f"{pending} item(s) still waiting for your approval.")
    else:
        body = (f"Today: {triggers} rule trigger(s), {trades} trade(s), "
                f"{pending} item(s) still waiting for your approval.")
    if llm_cost:
        # shadow-dual-active T7: `LLMUsage` (store/llm_usage.py) is process-
        # wide, not per-account -- both accounts' reflection/review calls
        # land in the same table -- so with one digest per account now
        # (scheduler.py's `_send_daily_digest`), this line is the SAME
        # combined total in both accounts' digests. "(all accounts)" says
        # so explicitly rather than reading as if this one account alone
        # spent that much.
        body += f"\nEstimated LLM cost today (all accounts): {llm_cost} (see Settings -> Usage)."
    return subject, body + FOOTER


def daily_report(*, account: str, date: str, summary: str, body: str) -> tuple[str, str]:
    """The end-of-day reflection notification (Phase 6). `summary` (the
    short push-friendly text the reflection itself produced) leads the full
    body so an email/console reader gets the punchy version before the full
    report; `send_report` (notify/base.py) is what routes `summary` alone
    to a push channel and this whole `full_body` to email/console."""
    subject = f"{_prefix(account)}[AllPath] Daily reflection {date}"
    full_body = f"{summary}\n\n{body}" + FOOTER
    return subject, full_body
