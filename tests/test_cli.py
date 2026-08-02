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


def test_serve_without_keys_exits_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["serve"])
    assert code == 2


def test_serve_starts_uvicorn_with_settings_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    created = {}
    run_calls = {}

    def fake_create_app(settings, **kwargs):
        created["settings"] = settings
        created["kwargs"] = kwargs
        return "THE-APP"

    def fake_run(app, host, port, log_level):
        run_calls.update(app=app, host=host, port=port, log_level=log_level)

    monkeypatch.setattr("allpath_trade.web.app.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", fake_run)

    code = main(["serve"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert run_calls == {"app": "THE-APP", "host": "127.0.0.1",
                         "port": 8791, "log_level": "warning"}
    assert created["kwargs"]["start_scheduler"] is True


def test_serve_host_and_port_override_settings(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    run_calls = {}

    monkeypatch.setattr("allpath_trade.web.app.create_app", lambda settings, **kw: "THE-APP")
    monkeypatch.setattr(
        "uvicorn.run",
        lambda app, host, port, log_level: run_calls.update(host=host, port=port))

    code = main(["serve", "--host", "0.0.0.0", "--port", "9000"],
               broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert run_calls == {"host": "0.0.0.0", "port": 9000}


def test_serve_prints_the_token_only_on_first_run(tmp_path, capsys, monkeypatch):
    # No .env yet -- first start of a fresh install must generate and print
    # the token so the operator can log in at all.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("allpath_trade.web.app.create_app", lambda settings, **kw: "THE-APP")
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, log_level: None)

    code = main(["serve"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    out = capsys.readouterr().out
    assert "[allpath-trade] access token: " in out
    assert "unchanged" not in out
    env_text = (tmp_path / ".env").read_text()
    assert "WEB_TOKEN=" in env_text


def test_serve_does_not_reprint_an_existing_token(tmp_path, capsys, monkeypatch):
    # A token already lives in .env from a previous run -- don't put it in
    # scrollback/log capture again on every subsequent start.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text('WEB_TOKEN="already-set-secret"\n')
    monkeypatch.setattr("allpath_trade.web.app.create_app", lambda settings, **kw: "THE-APP")
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, log_level: None)

    code = main(["serve"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    out = capsys.readouterr().out
    assert "already-set-secret" not in out
    assert "[allpath-trade] access token: unchanged" in out


def test_serve_ensures_token_before_constructing_the_app(tmp_path, monkeypatch):
    # ensure_token must run before create_app: create_app hands the Settings
    # instance down to components that read web_token later (the auth
    # middleware, in particular). If ensure_token ran after, a first-run
    # server would come up with an empty token baked into anything that
    # captured settings by value instead of by reference.
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_create_app(settings, **kwargs):
        seen["web_token"] = settings.web_token
        return "THE-APP"

    monkeypatch.setattr("allpath_trade.web.app.create_app", fake_create_app)
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, log_level: None)

    code = main(["serve"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert seen["web_token"] != ""
