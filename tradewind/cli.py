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


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradewind")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show account, positions, recent trades")
    args = parser.parse_args(argv)

    settings = SettingsStore().load()
    if args.command == "status":
        if broker_factory is None and not (
                settings.alpaca_api_key and settings.alpaca_secret_key):
            print("Missing credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                  "in .env (see .env.example)", file=sys.stderr)
            return 2
        broker = (broker_factory or _default_broker)(settings)
        return cmd_status(settings, broker)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
