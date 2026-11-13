# Strategy templates

Six starting points for your own strategies. They are **examples, not advice**: `XYZ` is a placeholder ticker, every price level assumes a $100 stock so the percentages are easy to read, and the sizes are arbitrary. Every template is a `draft` with `authorization: confirm` (or `notify`), so nothing is watched until you activate it and nothing trades without your click.

| Template | What it does |
|---|---|
| [`dip-buyer.yaml`](dip-buyer.yaml) | Opens a starter position after a 10% pullback, adds once at 15%, hard stop below. |
| [`pullback-add.yaml`](pullback-add.yaml) | Adds to an existing core holding on pullbacks, up to a weight cap. Shows a re-arming rule (`rearm`, `max_fires_per_day`). |
| [`take-profit-ladder.yaml`](take-profit-ladder.yaml) | Sells a quarter at +20%, +40% and +60% on cost; stop at -12%. |
| [`ratchet-stop.yaml`](ratchet-stop.yaml) | A hard stop you raise by revision as the position gains. Not an automatic trailing stop: the rule language has no "highest price since entry". |
| [`post-earnings-drift.yaml`](post-earnings-drift.yaml) | Written after a strong report: buy only above the gap level, leave if the gap fails. |
| [`price-alert-only.yaml`](price-alert-only.yaml) | `authorization: notify`: messages when a level is crossed, never an order. |

The longer single example is [`../strategy-example.yaml`](../strategy-example.yaml).

## Using a template

**Through chat (recommended).** Paste the YAML into the chat and say what to change, for example: "Draft a strategy from this template for AAPL, reference price 230, $1,000 per buy." The agent adjusts it and queues it on **Pending** as a diff. Nothing is saved until you approve.

**By hand.** Copy the file into your strategies folder under the account it is for, named after the id you want:

```bash
cp docs/examples/strategies/dip-buyer.yaml strategies/paper/aapl-dip-buyer.yaml
```

The file name is the strategy id (lowercase letters, digits and hyphens). Edit the ticker, levels and sizes, then open **Strategies**: the draft is listed there. A draft is not evaluated; set it to `active` on its page when you are ready. `strategies/` is your personal data and is git-ignored.

## The rule language in one minute

- **Conditions** compare these variables with numbers, joined by `and` / `or` / `not`: `price`, `position_weight` (a fraction of account equity: `0.10` is 10%), `target_weight`, `position_qty`, `avg_entry_price`, `pnl_pct` (percent gain on your average entry: `20` is +20%). There is no arithmetic and no indicator or date variables.
- **Actions**: `buy $2000`, `buy to target_weight`, `sell 25%`, `sell $1500`, `sell all`.
- **Rule type**: on an `auto` strategy a `hard` rule executes through the risk gate without review, and a `soft` rule is researched by the review agent first. On `confirm` both wait for you; on `notify` both only notify.
- **One-shot by default.** A rule fires once and stays `triggered` until a revision re-arms it. Add `rearm: 24h` (15 minutes to 7 days) and optionally `max_fires_per_day` to let it fire again. A re-arming buy must cap `position_weight` in its condition.
- **Authorization**: `notify` (message only), `confirm` (queued on Pending for your approval, the default), `auto` (executes within the risk gate). Start with `notify` or `confirm`.

Every template in this folder is loaded by `tests/test_strategy_templates.py` with the same parser the app uses, so they stay valid as the schema changes.
