"""Which accounts exist, and the one every store/table defaults to.

`account` is a first-class DIMENSION threaded through every account-scoped
store and query -- like `strategy_id` or `ticker` already are -- not a
config knob read once at startup. A config value lives in `Settings` and
picks behavior for the whole process; a dimension is carried on data and
checked at every read/write boundary, because both `paper` and `shadow`
are simultaneously live in the same process against the same tables.
"""

from __future__ import annotations

# Ordered so "paper" (the pre-existing, always-first account) prints/iterates
# first anywhere this tuple is walked -- dashboards, nightly-job loops, etc.
ACCOUNTS: tuple[str, ...] = ("paper", "shadow")

# The account every store constructor, table DEFAULT, and legacy-row backfill
# uses when none is given -- this app only ever had one account before the
# shadow account existed, so "unspecified" must mean the account that
# history already belongs to, not an ambiguous or global view.
DEFAULT_ACCOUNT = "paper"


def is_valid_account(account: str) -> bool:
    """True iff `account` is one of the known `ACCOUNTS`. Every boundary that
    accepts an account from outside the process (a cookie, a Telegram
    command, a CLI flag) must gate on this before it reaches a store -- an
    unvalidated value would still be safe from injection (every query below
    parameterizes it), but an unknown account string would silently create a
    fourth, ungoverned partition of the data instead of failing closed."""
    return account in ACCOUNTS
