from decimal import Decimal
from typing import ClassVar

from allpath_trade.broker.base import Account, Broker, Position
from allpath_trade.cli import cmd_chat, main
from allpath_trade.config import Settings


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


def test_status_recent_trade_line_labels_submission_not_fill(tmp_path, capsys, monkeypatch):
    # I1: `status` used to print a bare, unlabeled timestamp for a recent
    # trade -- the exact shape that caused the original 17-hour mislabel.
    # It must reuse the same "submitted"-labeled formatter as get_portfolio
    # and the system-prompt snapshot.
    from allpath_trade.broker.base import Order, OrderIntent, OrderSide, OrderStatus
    from allpath_trade.risk.gate import RiskDecision
    from allpath_trade.store.db import connect
    from allpath_trade.store.journal import TradeJournal

    monkeypatch.chdir(tmp_path)
    journal = TradeJournal(connect(tmp_path / "allpath-trade.db"))
    intent = OrderIntent(ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1), reason="dip")
    order = Order(id="o1", ticker="TSLA", side=OrderSide.BUY, qty=Decimal(1),
                 notional=None, status=OrderStatus.SUBMITTED, filled_qty=Decimal(0),
                 filled_avg_price=None, submitted_at="2026-08-09T20:27:00+00:00")
    journal.record(intent, RiskDecision(approved=True), order)

    code = main(["status"], broker_factory=lambda settings: FakeBroker())
    out = capsys.readouterr().out
    assert code == 0
    assert "submitted " in out
    assert "fill pending" in out


def test_status_without_keys_exits_2(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    code = main(["status"])  # no factory: builds from (empty) settings
    assert code == 2
    assert "ALPACA_API_KEY" in capsys.readouterr().err


def test_an_out_of_range_env_value_exits_2_with_a_readable_message_not_a_traceback(
        tmp_path, capsys, monkeypatch):
    # F1: CONTEXT_BUDGET_TOKENS was unconstrained before Finding 4 put a
    # floor on it (MIN_CONTEXT_BUDGET_TOKENS=2000, see config.py). A value
    # that used to be legal now fails Settings' own validation the moment
    # SettingsStore().load() runs inside main() -- unguarded, that's a raw
    # pydantic ValidationError traceback out of *every* command, including
    # "strategies", which needs no broker credentials at all.
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("CONTEXT_BUDGET_TOKENS=1000\n")
    code = main(["strategies"])
    err = capsys.readouterr().err
    assert code == 2
    assert "Traceback" not in err
    assert "context_budget_tokens" in err
    assert "greater than or equal to 2000" in err


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


# -- run: headless daemon's daily job (Task 5 parity with build_jobs) --
#
# cli's `run` daily job now goes through scheduler.run_daily_jobs, the same
# helper build_jobs uses (see test_scheduler.py's "-- build_jobs: daily
# digest email --" and "-- build_jobs / daily reflection --" sections for
# the behavior this dedupes against) -- so these fakes reuse test_scheduler.
# py's fixtures rather than redefining their own, and the tests below only
# cover what's specific to the `run` entry point (that it's actually wired
# through run_daemon's daily_job).


class _FakeRunReflector:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = 0

    def run_daily(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return "ok: report #1 (2026-08-10)"


class _FakeRunConsolidator:
    def __init__(self):
        self.calls = 0

    def run_daily(self):
        self.calls += 1
        return "ok"


def _patch_run_daemon_to_call_daily_job(monkeypatch, captured):
    def fake_run_daemon(sentinel_factory, interval, daily_job=None, app_state=None,
                        journal=None, broker=None):
        captured["daily_job"] = daily_job
        if daily_job is not None:
            daily_job()

    monkeypatch.setattr("allpath_trade.scheduler.run_daemon", fake_run_daemon)


def _fake_run_components(reflector=None, consolidator=None, daily_reflection=True,
                         daily_consolidation=True, notifier=None, journal=None,
                         queue=None, observations=None, app_state=None):
    from types import SimpleNamespace

    from tests.test_scheduler import (
        DigestNotifier,
        FakeAppState,
        FakeJournal,
        FakeObservations,
        FakeQueue,
    )

    return SimpleNamespace(
        strategies=None, sentinel=None, broker=None,
        queue=queue if queue is not None else FakeQueue(),
        journal=journal if journal is not None else FakeJournal(),
        app_state=app_state if app_state is not None else FakeAppState(),
        notifier=notifier if notifier is not None else DigestNotifier(),
        observations=observations if observations is not None else FakeObservations(),
        reflector=reflector, consolidator=consolidator,
        settings=SimpleNamespace(daily_reflection=daily_reflection,
                                 daily_consolidation=daily_consolidation))


def test_run_daily_job_runs_reflection_before_consolidation_isolated(
        tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    reflector = _FakeRunReflector(fail=True)
    consolidator = _FakeRunConsolidator()
    fake_components = _fake_run_components(reflector=reflector, consolidator=consolidator)
    monkeypatch.setattr("allpath_trade.app.build_components",
                        lambda settings, broker=None: fake_components)
    captured = {}
    _patch_run_daemon_to_call_daily_job(monkeypatch, captured)

    code = main(["run"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert captured["daily_job"] is not None
    # reflection ran (and raised) but consolidation still ran after it --
    # a broken reflection must not silently swallow consolidation.
    assert reflector.calls == 1
    assert consolidator.calls == 1
    assert "[reflection] failed" in capsys.readouterr().err


def test_run_daily_job_skips_reflection_when_setting_disabled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    reflector = _FakeRunReflector()
    consolidator = _FakeRunConsolidator()
    fake_components = _fake_run_components(reflector=reflector, consolidator=consolidator,
                                           daily_reflection=False)
    monkeypatch.setattr("allpath_trade.app.build_components",
                        lambda settings, broker=None: fake_components)
    captured = {}
    _patch_run_daemon_to_call_daily_job(monkeypatch, captured)

    code = main(["run"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert reflector.calls == 0
    assert consolidator.calls == 1  # not skipped just because reflection is off


def test_run_daily_job_skips_reflection_cleanly_when_no_reflector_configured(
        tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    consolidator = _FakeRunConsolidator()
    fake_components = _fake_run_components(reflector=None, consolidator=consolidator)
    monkeypatch.setattr("allpath_trade.app.build_components",
                        lambda settings, broker=None: fake_components)
    captured = {}
    _patch_run_daemon_to_call_daily_job(monkeypatch, captured)

    code = main(["run"], broker_factory=lambda settings: FakeBroker())

    assert code == 0  # must not raise
    assert consolidator.calls == 1


def test_run_daily_job_sends_digest(tmp_path, monkeypatch):
    # I2: `run`'s daily job never sent a digest at all before this round --
    # `_send_daily_digest` only ever hung off `serve`'s build_jobs. Now both
    # entry points funnel through the same scheduler.run_daily_jobs.
    monkeypatch.chdir(tmp_path)
    from tests.test_scheduler import DigestNotifier

    notifier = DigestNotifier()
    fake_components = _fake_run_components(notifier=notifier)
    monkeypatch.setattr("allpath_trade.app.build_components",
                        lambda settings, broker=None: fake_components)
    captured = {}
    _patch_run_daemon_to_call_daily_job(monkeypatch, captured)

    code = main(["run"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert len(notifier.sent) == 1


def test_run_daily_job_respects_daily_consolidation_gate(tmp_path, monkeypatch):
    # I2: `run`'s consolidation branch used to run unconditionally,
    # ignoring `daily_consolidation` -- build_jobs already gated it.
    monkeypatch.chdir(tmp_path)
    consolidator = _FakeRunConsolidator()
    fake_components = _fake_run_components(consolidator=consolidator,
                                           daily_consolidation=False)
    monkeypatch.setattr("allpath_trade.app.build_components",
                        lambda settings, broker=None: fake_components)
    captured = {}
    _patch_run_daemon_to_call_daily_job(monkeypatch, captured)

    code = main(["run"], broker_factory=lambda settings: FakeBroker())

    assert code == 0
    assert consolidator.calls == 0


def test_run_daily_job_digest_deduped_across_a_simulated_restart(tmp_path, monkeypatch):
    # I3: the digest's own app_state marker (digest_last_date) must survive
    # a process restart -- two separate `run` invocations sharing the same
    # (persisted) app_state on the same ET day must send only once.
    monkeypatch.chdir(tmp_path)
    from tests.test_scheduler import DigestNotifier, FakeAppState

    shared_app_state = FakeAppState()
    notifier = DigestNotifier()
    fake_components = _fake_run_components(notifier=notifier, app_state=shared_app_state)
    monkeypatch.setattr("allpath_trade.app.build_components",
                        lambda settings, broker=None: fake_components)
    captured = {}
    _patch_run_daemon_to_call_daily_job(monkeypatch, captured)

    main(["run"], broker_factory=lambda settings: FakeBroker())
    # "restart": a fresh call, but app_state (and its digest_last_date row)
    # survives, the same way a real sqlite-backed AppState would.
    main(["run"], broker_factory=lambda settings: FakeBroker())

    assert len(notifier.sent) == 1


class SpyCompactor:
    """Stands in for allpath_trade.agent.compact.Compactor so a test can
    inspect what cmd_chat constructed it with, without needing a real LLM
    call or a conversation big enough to trigger compaction."""

    instances: ClassVar[list["SpyCompactor"]] = []

    def __init__(self, llm, store, budget_tokens=60_000, on_before_compact=None):
        self.llm = llm
        self.store = store
        self.budget_tokens = budget_tokens
        self.on_before_compact = on_before_compact
        self.calls = 0
        SpyCompactor.instances.append(self)

    def maybe_compact(self, conversation_id, history):
        self.calls += 1
        return list(history), history


def test_chat_wires_a_compactor_from_the_configured_context_budget(tmp_path, monkeypatch):
    # cmd_chat resumes the same unbounded conversation the web chat does
    # (allpath_trade/cli.py), so it needs the same Compactor the web wires
    # in ChatService._build -- otherwise a long-lived terminal session grows
    # its context forever.
    from allpath_trade.app import build_components
    from allpath_trade.llm.base import LLMResponse
    from tests.test_agent_loop import ScriptedLLM

    monkeypatch.chdir(tmp_path)
    SpyCompactor.instances = []
    monkeypatch.setattr("allpath_trade.agent.compact.Compactor", SpyCompactor)

    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory",
                        openrouter_api_key="k", context_budget_tokens=12345)
    settings.strategies_dir.mkdir()
    llm = ScriptedLLM([LLMResponse(text="hi there")])
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat": llm)
    components = build_components(settings, broker=FakeBroker())

    inputs = iter(["hello", "/exit"])
    code = cmd_chat(components, llm, new=False,
                    input_fn=lambda prompt="": next(inputs))

    assert code == 0
    assert len(SpyCompactor.instances) == 1
    assert SpyCompactor.instances[0].budget_tokens == 12345
    # Finding 7: constructing a Compactor and never using it would also
    # satisfy the assertions above. AgentSession.run_turn calls
    # maybe_compact once per iteration whenever a compactor is wired in
    # (allpath_trade/agent/loop.py) -- the "hello" turn above must have
    # actually reached it, proving the object built here is the same one
    # AgentSession runs against, not one built and dropped on the floor.
    assert SpyCompactor.instances[0].calls >= 1


def test_chat_wires_the_consolidator_flush_hook_into_the_compactor(tmp_path, monkeypatch):
    # Finding 8: without this, on_before_compact is dead code -- no
    # production caller passes it, so a preference the user states once,
    # early in a long conversation, can be compacted away with nothing ever
    # writing it to curated memory.
    from allpath_trade.app import build_components
    from allpath_trade.llm.base import LLMResponse
    from tests.test_agent_loop import ScriptedLLM

    monkeypatch.chdir(tmp_path)
    SpyCompactor.instances = []
    monkeypatch.setattr("allpath_trade.agent.compact.Compactor", SpyCompactor)

    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", openrouter_api_key="k")
    settings.strategies_dir.mkdir()
    llm = ScriptedLLM([LLMResponse(text="hi there")])
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat": llm)
    components = build_components(settings, broker=FakeBroker())
    assert components.consolidator is not None  # sanity: the hook has something to bind to

    inputs = iter(["hello", "/exit"])
    code = cmd_chat(components, llm, new=False,
                    input_fn=lambda prompt="": next(inputs))

    assert code == 0
    assert len(SpyCompactor.instances) == 1
    hook = SpyCompactor.instances[0].on_before_compact
    assert hook is not None
    # F2: the hook is now a `functools.partial` binding `propagate=True`
    # (was the bare bound method) -- Compactor's own try/except can only
    # treat a flush failure as "skip compaction, keep the messages" when the
    # hook actually raises, and run_post_chat's default swallows failures
    # into a string return instead. See Consolidator.run_post_chat's
    # docstring.
    assert hook.func == components.consolidator.run_post_chat
    assert hook.keywords == {"propagate": True}


def test_chat_consolidation_survives_a_mid_session_compaction(tmp_path, monkeypatch):
    """Finding 6: cmd_chat's post-chat consolidation used to slice
    `session.history[initial_len:]` -- a plain list index computed before
    the loop starts. run_turn (agent/loop.py) reassigns `session.history` to
    whatever trimmed tail Compactor.maybe_compact returns, so once a
    compaction runs mid-session, `len(session.history)` can drop below
    `initial_len` (especially when resuming a conversation that already had
    history) and the slice silently yields `[]` -- consolidation looks like
    it ran but records nothing, right on the longest conversations, the ones
    with the most worth remembering.

    Reproduces that exactly: seed a conversation with prior history from an
    "earlier session" (giving cmd_chat a nonzero initial_len), pick a tiny
    context budget so the very next turn forces a compaction that cuts the
    in-memory history down below that count, then confirm the post-chat
    consolidator still receives this session's new turns.
    """
    from allpath_trade.app import build_components
    from allpath_trade.config import SettingsStore
    from allpath_trade.llm.base import LLMResponse
    from allpath_trade.store.conversations import ConversationStore
    from tests.test_agent_loop import ScriptedLLM, tool_response

    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / ".env").write_text(
        'OPENROUTER_API_KEY="k"\nCONTEXT_BUDGET_TOKENS="2500"\n')

    consolidate_llm = ScriptedLLM([
        tool_response("memory_update",
                      {"layer": "profile", "action": "add",
                       "text": "prefers dividends"}),
        LLMResponse(text="noted 1 preference"),
    ])
    # build_components builds the consolidator's own LLM straight from
    # build_llm(settings, tier="memory") -- patch that globally so the
    # consolidator (not cmd_chat's `memory_llm` argument, which is separate
    # and injected below) uses the scripted client.
    monkeypatch.setattr("allpath_trade.llm.factory.build_llm",
                        lambda settings, tier="chat": consolidate_llm)

    settings = SettingsStore().load()
    components = build_components(settings, broker=FakeBroker())

    # Seed history from a "previous session": four big messages already
    # committed to the store before cmd_chat ever starts, so resuming this
    # conversation gives cmd_chat a nonzero initial_len.
    store = ConversationStore(components.conn)
    cid = store.start()
    for i in range(4):
        store.append(cid, {"role": "user" if i % 2 == 0 else "assistant",
                           "content": "x" * 6000})

    chat_llm = ScriptedLLM([LLMResponse(text="ok, noted")])
    # The Compactor's own summarizing LLM (distinct from the consolidator's).
    compactor_llm = ScriptedLLM([LLMResponse(text="earlier: a long-running chat")])

    lines = iter(["remember I prefer dividends"])

    def input_then_eof(*a):
        try:
            return next(lines)
        except StopIteration:
            raise EOFError

    code = cmd_chat(components, chat_llm, new=False, input_fn=input_then_eof,
                    memory_llm=compactor_llm)

    assert code == 0
    # Sanity: the scenario must actually force a compaction, or this test
    # would pass vacuously without ever exercising the bug.
    assert len(compactor_llm.seen) >= 1

    memory_file = tmp_path / "memory" / "user_profile.md"
    assert memory_file.exists(), (
        "post-chat consolidation never ran -- the stale initial_len slice "
        "silently dropped this session's only new turn")
    assert "dividends" in memory_file.read_text()
