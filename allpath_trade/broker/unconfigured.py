from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from allpath_trade.broker.base import (
    Account,
    Broker,
    BrokerNotConfigured,
    Order,
    OrderIntent,
    Position,
)

# The single message every method raises with. One constant, not eight
# copies: this string reaches the user through several different surfaces
# (the dashboard's "Broker unavailable: ..." slot, a sentinel report error
# line, a chat tool result), and they must all say the same thing and point
# at the same next step.
_MESSAGE = "Alpaca keys are not set — finish setup"


class UnconfiguredBroker(Broker):
    """Placeholder for the `paper` account before any Alpaca credentials
    exist -- what `app._build_broker` returns when either key is empty.

    Its whole purpose is to let `allpath-trade serve` START on a fresh
    install. `AlpacaBroker`'s constructor needs credentials, so before this
    existed the only way to keep the process from blowing up was to refuse
    to start at all (cli.py exited 2 with "Missing credentials") -- which
    made the first-run setup wizard, a web page served by that very
    process, unreachable: the user needed the app running to enter the keys
    and needed the keys to run the app.

    So this is deliberately NOT a null object that returns empty accounts
    and empty position lists. A zero-equity `Account` would flow straight
    into the risk gate, the sentinel's weight math, and the dashboard as if
    it were a real reading of a real (empty) brokerage account, and every
    one of those would then make confident, wrong statements about a
    portfolio nobody has connected yet. Raising `BrokerNotConfigured`
    instead makes "not set up" impossible to mistake for "set up and
    empty": callers either handle the setup state explicitly (see
    sentinel.run_once, scheduler._run_sentinel_pass, the dashboard's
    heartbeat line) or fall into the "broker unavailable" path they already
    have for a dead broker, which is the right degradation either way.

    `is_paper = True` matches the account it stands in for (and keeps any
    "LIVE" wording off a screen where nothing is connected at all).
    """

    name = "unconfigured"
    is_paper = True

    def get_account(self) -> Account:
        raise BrokerNotConfigured(_MESSAGE)

    def get_positions(self) -> list[Position]:
        raise BrokerNotConfigured(_MESSAGE)

    def get_order(self, order_id: str) -> Order:
        raise BrokerNotConfigured(_MESSAGE)

    def get_orders(self, open_only: bool = True) -> list[Order]:
        raise BrokerNotConfigured(_MESSAGE)

    def submit_order(self, intent: OrderIntent) -> Order:
        raise BrokerNotConfigured(_MESSAGE)

    def cancel_order(self, order_id: str) -> None:
        raise BrokerNotConfigured(_MESSAGE)

    def get_equity_history(self, days: int) -> list[tuple[datetime, Decimal]]:
        # Overrides the base class's non-abstract `return []` (see
        # Broker.get_equity_history): inheriting it would have this one
        # method quietly answer "no history" -- indistinguishable from a
        # real, connected account whose history is genuinely empty -- while
        # every other method says "not configured". Same reasoning as the
        # class docstring's: never let "not set up" read as "set up and
        # empty". The dashboard's own caller already degrades any raise
        # here to the "No history yet" placeholder.
        raise BrokerNotConfigured(_MESSAGE)
