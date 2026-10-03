# Agent feed: driving AllPath Trade from another agent

AllPath Trade's notifications (email, ntfy, Telegram) are written for a human.
An external executor agent should use these machine-readable surfaces instead:

1. **A wake-up signal**: the agent webhook (`AGENT_WEBHOOK_URL`).
2. **The source of truth plus the controls**: the CLI with `--json`, run on the
   host where `serve` runs (for example over SSH).

The webhook only tells the agent that *something happened*. The agent must
always re-read state through the CLI before it acts: events can be dropped,
duplicated, or out of date by the time they arrive.

## Configuration (`.env`, restart `serve` afterwards)

```dotenv
AGENT_WEBHOOK_URL=https://example.com/hooks/allpath   # or https://ntfy.sh/<private-topic>
AGENT_WEBHOOK_TOKEN=                                  # optional, sent as "Authorization: Bearer <token>"
```

If you use ntfy, pick a topic that is separate from your phone's topic and
protect it with an access token: anyone who knows the name of a public
ntfy.sh topic can read it.

## Event payload

Every notification the app sends is POSTed as JSON (`Content-Type:
application/json`, `X-AllPath-Event: <type>`, `Title: <subject>`, where a
non-ASCII title is RFC 2047-encoded):

```json
{"v": 1, "ts": "2026-09-26T16:00:00+00:00", "type": "review_queued",
 "account": "paper", "review_id": 70, "kind": "option_order", "ticker": "MSFT",
 "strategy_id": "msft-diversifier-swing", "action": "buy_call $1200 dte>=30 otm=4%",
 "next": "allpath-trade reviews --account paper list --json",
 "subject": "[Paper] [AllPath] MSFT: waiting for your approval"}
```

| `type` | Extra fields |
|---|---|
| `review_queued` | `review_id`, `kind` (`order` / `option_order` / `strategy_revision` / `shadow_edit`), `ticker`, `strategy_id`, `action`, `next` |
| `rule_triggered` | `strategy_id`, `rule_id`, `ticker`, `condition`, `disposition` |
| `order_result` | `ticker`, `side`, `submitted`, `recorded_only` (true for shadow: nothing was routed), `filled_qty`, `filled_avg_price` |
| `drawdown_halt` | `peak`, `equity`, `drawdown`, `demoted` |
| `daily_digest` | `triggers`, `trades`, `pending` |
| `daily_report` | `date` |
| `notification` | none (a message with no structured event, e.g. a test send) |

Money values are strings. The payload never contains the message body, an
approve link, or any token. `notify_email: false` on a strategy mutes the human
channels only; it never mutes this feed.

## Acting

```bash
allpath-trade reviews --account paper list --json
allpath-trade reviews --account paper approve <id>
allpath-trade reviews --account paper reject <id> --note "<reason>"
allpath-trade status --account paper --json
```

`approve` goes through the same path as the web UI: it re-prices the order,
re-runs the risk gate, and refuses option orders outside market hours (the
item then stays pending). On the `shadow` account, approving only records the
change in the ledger; a human places the real trade.
