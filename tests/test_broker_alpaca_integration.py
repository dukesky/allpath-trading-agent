"""Real Alpaca paper-account round trip. Runs only with credentials:
    uv run pytest -m integration tests/test_broker_alpaca_integration.py -v
"""
import os

import pytest

from allpath_trade.broker.alpaca import AlpacaBroker

pytestmark = pytest.mark.integration

needs_keys = pytest.mark.skipif(
    not (os.getenv("ALPACA_API_KEY") and os.getenv("ALPACA_SECRET_KEY")),
    reason="ALPACA_API_KEY / ALPACA_SECRET_KEY not set",
)


@needs_keys
def test_paper_account_roundtrip():
    b = AlpacaBroker(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True)
    acct = b.get_account()
    assert acct.equity > 0
    assert isinstance(b.get_positions(), list)
    assert isinstance(b.get_orders(open_only=True), list)
