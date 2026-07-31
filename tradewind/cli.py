from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from tradewind.broker.base import Broker
from tradewind.config import Settings, SettingsStore
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal


def _default_broker(settings: Settings) -> Broker:
    from tradewind.broker.alpaca import AlpacaBroker

    return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                        paper=settings.alpaca_paper)


def cmd_status(settings: Settings, broker: Broker) -> int:
    try:
        acct = broker.get_account()
        mode = "PAPER" if broker.is_paper else "LIVE"
        print(f"[{broker.name} / {mode.lower()}]")
        print(f"equity: {acct.equity}  cash: {acct.cash}  buying_power: {acct.buying_power}")
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit, never crash
        print(f"Could not reach broker: {exc}. Check your Alpaca keys in .env.",
              file=sys.stderr)
        return 1

    if positions:
        print("\npositions:")
        for p in positions:
            print(f"  {p.ticker:6} qty={p.qty} avg={p.avg_entry_price} "
                  f"value={p.market_value} pl={p.unrealized_pl}")
    else:
        print("\nno open positions")

    journal = TradeJournal(connect(settings.db_path))
    rows = journal.recent(limit=5)
    if rows:
        print("\nrecent trades:")
        for r in rows:
            print(f"  #{r['id']} {r['ts'][:19]} {r['side']} {r['ticker']} "
                  f"[{r['status']}] {r['reason']}")
    return 0


def cmd_check(components) -> int:
    report = components.sentinel.run_once()
    print(f"strategies checked: {report.strategies_checked}")
    for o in report.outcomes:
        print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition}"
              + (f" ({o.detail})" if o.detail else ""))
    for e in report.errors:
        print(f"  error: {e}", file=sys.stderr)
    return 1 if report.errors else 0


def cmd_strategies(components) -> int:
    errors: list[str] = []
    docs = components.strategies.load_all(status=None, errors=errors)
    for err in errors:
        print(f"warning: {err}", file=sys.stderr)
    if not docs:
        print("no strategies found in", components.settings.strategies_dir)
        return 0
    for d in docs:
        print(f"{d.id}  [{d.status.value}/{d.authorization.value}]  {d.name}")
        for r in d.rules:
            print(f"    {r.id} ({r.type.value}, {r.state.value}): "
                  f"{r.condition} -> {r.action}")
    return 0


def cmd_rearm(components, strategy_id: str, rule_id: str) -> int:
    from tradewind.strategy.loader import StrategyValidationError

    try:
        doc = components.strategies.load(strategy_id)
    except FileNotFoundError:
        print(f"strategy '{strategy_id}' not found", file=sys.stderr)
        return 1
    except StrategyValidationError as exc:
        print(f"strategy '{strategy_id}' is invalid: {exc}", file=sys.stderr)
        return 1
    if rule_id not in {r.id for r in doc.rules}:
        print(f"rule {rule_id} not found in {strategy_id}", file=sys.stderr)
        return 1
    components.strategies.rearm(strategy_id, rule_id)
    print(f"{strategy_id}/{rule_id} re-armed")
    return 0


def cmd_reviews(components, args) -> int:
    from tradewind.store.reviews import ReviewError

    q = components.queue
    try:
        if args.reviews_command == "list":
            rows = q.list()
            if not rows:
                print("no pending reviews")
            for r in rows:
                print(f"#{r['id']} {r['ts'][:19]} {r['strategy_id']}/{r['rule_id']} "
                      f"[{r['status']}] {r['condition']} -> {r['action']}")
        elif args.reviews_command == "approve":
            result = q.approve(args.review_id)
            print("executed" if result.submitted
                  else f"rejected by risk gate: {'; '.join(result.decision.reasons)}")
        else:
            q.reject(args.review_id, note=args.note or "")
            print(f"review {args.review_id} rejected")
        return 0
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradewind")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show account, positions, recent trades")
    sub.add_parser("check", help="run one sentinel pass now")
    sub.add_parser("run", help="run the sentinel daemon")
    sub.add_parser("strategies", help="list strategies and rule states")
    p_rearm = sub.add_parser("rearm", help="re-arm a triggered rule")
    p_rearm.add_argument("strategy_id")
    p_rearm.add_argument("rule_id")
    p_reviews = sub.add_parser("reviews", help="pending trigger reviews")
    rsub = p_reviews.add_subparsers(dest="reviews_command", required=True)
    rsub.add_parser("list")
    p_app = rsub.add_parser("approve")
    p_app.add_argument("review_id", type=int)
    p_rej = rsub.add_parser("reject")
    p_rej.add_argument("review_id", type=int)
    p_rej.add_argument("--note", default="")
    args = parser.parse_args(argv)

    settings = SettingsStore().load()
    if broker_factory is None and not (
            settings.alpaca_api_key and settings.alpaca_secret_key):
        print("Missing credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY "
              "in .env (see .env.example)", file=sys.stderr)
        return 2
    broker = (broker_factory or _default_broker)(settings)

    if args.command == "status":
        return cmd_status(settings, broker)

    from tradewind.app import build_components

    components = build_components(settings, broker=broker)
    if args.command == "check":
        return cmd_check(components)
    if args.command == "run":
        from tradewind.scheduler import run_daemon

        run_daemon(lambda: components.sentinel,
                   settings.sentinel_interval_minutes)
        return 0
    if args.command == "strategies":
        return cmd_strategies(components)
    if args.command == "rearm":
        return cmd_rearm(components, args.strategy_id, args.rule_id)
    if args.command == "reviews":
        return cmd_reviews(components, args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
