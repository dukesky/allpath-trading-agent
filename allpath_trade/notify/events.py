from __future__ import annotations

FOOTER = ("\n\nOpen the AllPath Trade dashboard to act on this. "
          "This message contains no links by design — an emailed link that "
          "carries your access token would turn a leaked inbox into a leaked "
          "account.")


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
                  strategy_id: str, recommendation: str = "") -> tuple[str, str]:
    subject = f"[AllPath] {ticker}: waiting for your approval"
    lines = [f"Item #{review_id} is waiting for you.",
             f"Proposed: {action} on {ticker}"]
    if strategy_id:
        lines.append(f"Strategy: {strategy_id}")
    if recommendation:
        lines.append(f"The agent recommends: {recommendation}")
    return subject, "\n".join(lines) + FOOTER


def daily_digest(*, triggers: int, trades: int, pending: int) -> tuple[str, str]:
    subject = "[AllPath] Daily summary"
    body = (f"Today: {triggers} rule trigger(s), {trades} trade(s), "
            f"{pending} item(s) still waiting for your approval." + FOOTER)
    return subject, body
