from pathlib import Path

from allpath_trade.cli import main
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker  # reuse fixture broker

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


VALID_REVISION_YAML = """\
name: "T"
status: active
version: 2
authorization: notify
position: {ticker: AAPL, target_weight: 10%}
rules:
  - {id: r1, type: hard, condition: "price < 90000", action: "sell all"}
"""


def _queue_revision(tmp_path, new_yaml=VALID_REVISION_YAML) -> int:
    from allpath_trade.store.db import connect
    from allpath_trade.store.reviews import ReviewQueue

    # The applier's staleness gate (Finding 1) compares the file's CURRENT
    # text against the proposal's recorded base, so `old_yaml` has to be
    # the strategy file's actual on-disk content for `approve` to succeed.
    old_yaml = (tmp_path / "strategies" / "t.yaml").read_text()
    conn = connect(tmp_path / "allpath-trade.db")
    rid = ReviewQueue(conn, executor=None).add_strategy_revision(
        strategy_id="t", ticker="AAPL", old_yaml=old_yaml, new_yaml=new_yaml,
        diff="d", rationale="reflection rationale")
    conn.close()
    return rid


def test_reviews_approve_on_a_revision_row_applies_it(tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    rid = _queue_revision(tmp_path)

    code = main(["reviews", "approve", str(rid)], broker_factory=lambda s: FakeBroker())

    out = capsys.readouterr().out
    assert code == 0  # not a crash from `result.submitted` on None
    assert "Revision applied to t." in out
    # shadow-dual-active T2: build_components (reached via main()'s broker
    # branch above) migrates the legacy strategies/t.yaml this fixture wrote
    # into strategies/paper/t.yaml before the approve applier ever runs.
    assert (tmp_path / "strategies" / "paper" / "t.yaml").read_text() == \
        VALID_REVISION_YAML


def test_reviews_approve_warns_when_the_revised_rule_is_still_triggered(
        tmp_path, capsys, monkeypatch):
    # Finding F2, CLI-side mirror of the web approve flow's warning.
    from allpath_trade.store.db import connect
    from allpath_trade.strategy.model import RuleState
    from allpath_trade.strategy.store import StrategyStore

    setup_env(tmp_path, monkeypatch)
    conn = connect(tmp_path / "allpath-trade.db")
    StrategyStore(tmp_path / "strategies", conn).set_rule_state("t", "r1", RuleState.TRIGGERED)
    conn.close()
    rid = _queue_revision(tmp_path)

    code = main(["reviews", "approve", str(rid)], broker_factory=lambda s: FakeBroker())

    out = capsys.readouterr().out
    assert code == 0
    assert "Revision applied to t." in out
    assert "r1 is still triggered" in out
    assert "re-arm" in out


def test_reviews_approve_on_a_stale_revision_leaves_it_pending(
        tmp_path, capsys, monkeypatch):
    setup_env(tmp_path, monkeypatch)
    # Missing `position` -- fails re-validation in the applier.
    rid = _queue_revision(tmp_path, new_yaml="name: Bad\nstatus: active\n")

    code = main(["reviews", "approve", str(rid)], broker_factory=lambda s: FakeBroker())

    err = capsys.readouterr().err
    assert code == 1
    assert "pending" in err.lower()

    from allpath_trade.store.db import connect
    from allpath_trade.store.reviews import ReviewQueue

    conn = connect(tmp_path / "allpath-trade.db")
    row = ReviewQueue(conn, executor=None).get(rid)
    assert row["status"] == "pending"
    conn.close()


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


def test_reviews_approve_order_row_still_requires_credentials(tmp_path, capsys, monkeypatch):
    # Regression guard for the Finding 6 fix: relaxing the credential
    # requirement for strategy_revision rows must not relax it for
    # order-kind rows too.
    setup_env(tmp_path, monkeypatch)
    (tmp_path / "strategies" / "t.yaml").write_text(
        STRAT.replace("authorization: notify", "authorization: confirm"))
    main(["check"], broker_factory=lambda s: FakeBroker())  # queues review #1 (order)
    _clear_alpaca_env(monkeypatch)

    code = main(["reviews", "approve", "1"])  # no broker_factory, no keys

    assert code == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_reviews_approve_revision_works_without_credentials(tmp_path, capsys, monkeypatch):
    # Finding 6: approving a strategy_revision row is pure file I/O (no
    # broker call anywhere on that path -- see
    # ReviewQueue._approve_revision) and must not demand Alpaca credentials.
    setup_env(tmp_path, monkeypatch)
    rid = _queue_revision(tmp_path)
    _clear_alpaca_env(monkeypatch)

    code = main(["reviews", "approve", str(rid)])  # no broker_factory, no keys

    out = capsys.readouterr().out
    assert code == 0
    assert "Revision applied to t." in out
    # shadow-dual-active T2: build_components (reached via main()'s broker
    # branch above) migrates the legacy strategies/t.yaml this fixture wrote
    # into strategies/paper/t.yaml before the approve applier ever runs.
    assert (tmp_path / "strategies" / "paper" / "t.yaml").read_text() == \
        VALID_REVISION_YAML


def test_cli_output_is_english_only(tmp_path, capsys, monkeypatch):
    # B4: the shared English-only invariant applied to a non-web surface --
    # every command below prints through a different code path (status
    # lines, table rows, friendly-error messages, a missing-credentials
    # exit), and CLI output was the one surface in the whole app the
    # invariant had never touched at all.
    setup_env(tmp_path, monkeypatch)
    main(["check"], broker_factory=lambda s: FakeBroker())
    main(["strategies"], broker_factory=lambda s: FakeBroker())
    main(["rearm", "t", "r1"], broker_factory=lambda s: FakeBroker())
    main(["reviews", "list"], broker_factory=lambda s: FakeBroker())
    main(["rearm", "nope", "r1"], broker_factory=lambda s: FakeBroker())
    _clear_alpaca_env(monkeypatch)
    main(["check"])

    # F4: `chat`'s banner and hint copy (CHAT_BANNER, the model/account line,
    # the "orders & strategy changes always ask first" hint) print through
    # rich's Console rather than the plain print() every command above uses
    # -- a separate code path B4's original sweep never touched.
    from allpath_trade.llm.base import LLMResponse
    from tests.test_agent_loop import ScriptedLLM

    monkeypatch.setenv("ALPACA_API_KEY", "k")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
    chat_lines = iter(["hello", "/exit"])
    monkeypatch.setattr("builtins.input", lambda *a: next(chat_lines))
    main(["chat"], broker_factory=lambda s: FakeBroker(),
        llm_factory=lambda s, tier: ScriptedLLM([LLMResponse(text="hi there")]))

    captured = capsys.readouterr()
    assert_english_only(captured.out)
    assert_english_only(captured.err)
