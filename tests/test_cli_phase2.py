from pathlib import Path

from tests.test_sentinel import FakeBroker  # reuse fixture broker
from tradewind.cli import main

STRAT = """
name: "T"
status: active
authorization: notify
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100000", action: "sell all"}
"""


def setup_env(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "t.yaml").write_text(STRAT)


def test_check_command_runs_and_reports(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    code = main(["check"], broker_factory=lambda s: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "t/r1" in out and "notified" in out


def test_strategies_command_lists_rules(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    code = main(["strategies"], broker_factory=lambda s: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "t" in out and "r1" in out and "armed" in out


def test_rearm_command(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    main(["check"], broker_factory=lambda s: FakeBroker())  # triggers r1
    code = main(["rearm", "t", "r1"], broker_factory=lambda s: FakeBroker())
    assert code == 0
    out = capsys.readouterr().out
    assert "armed" in out


def test_rearm_missing_strategy_prints_friendly_message(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    code = main(["rearm", "nope", "r1"], broker_factory=lambda s: FakeBroker())
    err = capsys.readouterr().err
    assert code == 1
    assert "nope" in err
    assert "Traceback" not in err


def test_rearm_invalid_strategy_prints_friendly_message(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "strategies" / "bad.yaml").write_text("name: x\nstatus: active\n")
    code = main(["rearm", "bad", "r1"], broker_factory=lambda s: FakeBroker())
    err = capsys.readouterr().err
    assert code == 1
    assert "bad" in err
    assert "Traceback" not in err


def test_strategies_command_reports_bad_yaml_and_still_lists_good(
        tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "strategies" / "bad.yaml").write_text("name: x\nstatus: active\n")
    code = main(["strategies"], broker_factory=lambda s: FakeBroker())
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert code == 0
    assert "t" in out and "r1" in out
    assert "bad.yaml" in err


def test_reviews_flow(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "strategies" / "t.yaml").write_text(
        STRAT.replace("authorization: notify", "authorization: confirm"))
    main(["check"], broker_factory=lambda s: FakeBroker())
    main(["reviews", "list"], broker_factory=lambda s: FakeBroker())
    out = capsys.readouterr().out
    assert "pending" in out
    code = main(["reviews", "reject", "1", "--note", "no"],
                broker_factory=lambda s: FakeBroker())
    assert code == 0


def _clear_alpaca_env(monkeypatch):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)


def test_strategies_works_without_credentials(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    _clear_alpaca_env(monkeypatch)
    code = main(["strategies"])  # no broker_factory, no keys
    out = capsys.readouterr().out
    assert code == 0
    assert "t" in out and "r1" in out


def test_rearm_and_reviews_list_work_without_credentials(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    _clear_alpaca_env(monkeypatch)
    assert main(["reviews", "list"]) == 0
    assert "no pending reviews" in capsys.readouterr().out
    code = main(["rearm", "t", "nope"])
    assert code == 1  # rule not found — but reached the handler, not the gate


def test_check_still_requires_credentials(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    _clear_alpaca_env(monkeypatch)
    assert main(["check"]) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_reviews_approve_still_requires_credentials(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    _clear_alpaca_env(monkeypatch)
    assert main(["reviews", "approve", "1"]) == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err
