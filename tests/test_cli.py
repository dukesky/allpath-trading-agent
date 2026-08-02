from decimal import Decimal

from allpath_trade.broker.base import Account, Broker, Position
from allpath_trade.cli import main


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(4000),
                       buying_power=Decimal(8000))

    def get_positions(self):
        return [Position(ticker="AAPL", qty=Decimal(5),
                         avg_entry_price=Decimal(190),
                         market_value=Decimal(1000),
                         unrealized_pl=Decimal(50))]

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


def test_status_prints_account_and_positions(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)  # so allpath_trade.db lands in tmp
    code = main(["status"], broker_factory=lambda settings: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "10000" in out and "AAPL" in out and "paper" in out.lower()


def test_status_without_keys_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["status"])  # no factory: builds from (empty) settings
    assert code == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


class RaisingBroker(Broker):
    name = "fake"
    is_paper = True

    def get_account(self):
        raise RuntimeError("connection refused")

    def get_positions(self):
        return []

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


def test_status_broker_error_prints_friendly_message_and_returns_1(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code = main(["status"], broker_factory=lambda settings: RaisingBroker())
    err = capsys.readouterr().err
    assert code == 1
    assert "Could not reach broker" in err
    assert "connection refused" in err
