from __future__ import annotations

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


def rule_triggered(*, strategy_id: str, rule_id: str, ticker: str,
                   condition: str, disposition: str) -> tuple[str, str]:
    subject = f"[AllPath] {ticker}: rule {rule_id} triggered"
    body = (f"Strategy {strategy_id}, rule {rule_id} triggered on {ticker}.\n"
            f"Condition: {condition}\n"
            f"Disposition: {disposition}." + FOOTER)
    return subject, body


def order_result(*, ticker: str, side: str, submitted: bool,
                 detail: str) -> tuple[str, str]:
    outcome = "submitted" if submitted else "not submitted"
    subject = f"[AllPath] {ticker}: order {outcome}"
    body = f"A {side} order for {ticker} was {outcome}.\n{detail}" + FOOTER
    return subject, body


def review_queued(*, review_id: int, ticker: str, action: str,
                  strategy_id: str, recommendation: str = "",
                  trigger_price: str = "", est_shares: str = "",
                  approve_url: str = "") -> tuple[str, str]:
    """`trigger_price`/`est_shares` are the price context available at the
    instant this item was queued (Part B) -- the same sample the rule
    triggered on, not a second, separately-fetched "live" quote (the
    sentinel has no cheap way to requote here, and a second number this
    close in time to the first would only look like independent
    confirmation it isn't). `approve_url`, when non-empty, is the one place
    a notification body ever carries a link -- see FOOTER's docstring for
    why that's still opt-in and safe to describe truthfully there."""
    subject = f"[AllPath] {ticker}: waiting for your approval"
    lines = [f"Item #{review_id} is waiting for you.",
             f"Proposed: {action} on {ticker}"]
    if strategy_id:
        lines.append(f"Strategy: {strategy_id}")
    if trigger_price:
        lines.append(f"Price at trigger: {trigger_price}")
    if est_shares:
        lines.append(f"Est. size: ~{est_shares} shares at that price")
    if recommendation:
        lines.append(f"The agent recommends: {recommendation}")
    body = "\n".join(lines)
    if approve_url:
        body += f"\nReview & approve: {approve_url}"
    return subject, body + FOOTER


def daily_digest(*, triggers: int, trades: int, pending: int) -> tuple[str, str]:
    subject = "[AllPath] Daily summary"
    body = (f"Today: {triggers} rule trigger(s), {trades} trade(s), "
            f"{pending} item(s) still waiting for your approval." + FOOTER)
    return subject, body


def daily_report(*, date: str, summary: str, body: str) -> tuple[str, str]:
    """The end-of-day reflection notification (Phase 6). `summary` (the
    short push-friendly text the reflection itself produced) leads the full
    body so an email/console reader gets the punchy version before the full
    report; `send_report` (notify/base.py) is what routes `summary` alone
    to a push channel and this whole `full_body` to email/console."""
    subject = f"[AllPath] Daily reflection {date}"
    full_body = f"{summary}\n\n{body}" + FOOTER
    return subject, full_body
