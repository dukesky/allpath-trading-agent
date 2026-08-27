"""shadow-dual-active T4: dual AccountComponents wiring via build_components.

Covers: both accounts built with distinct brokers (paper=injected fake
stand-in for Alpaca, shadow=ShadowLedger); the legacy single-account
attribute surface on `Components` aliases `accounts["paper"]` exactly (every
existing web/cli/telegram consumer keeps working unchanged this task); each
account's strategies directory is genuinely separate; and the executor/
applier wiring is per account -- approving a shadow strategy_revision writes
only shadow's strategies directory, never paper's."""

from decimal import Decimal

from allpath_trade.app import build_components
from allpath_trade.broker.base import Account, Broker, Position
from allpath_trade.broker.shadow import ShadowLedger
from allpath_trade.config import Settings


class FakeBroker(Broker):
    name = "fake"
    is_paper = True

    def get_account(self):
        return Account(equity=Decimal(10000), cash=Decimal(5000),
                       buying_power=Decimal(10000))

    def get_positions(self) -> list[Position]:
        return []

    def get_order(self, order_id):
        raise NotImplementedError

    def get_orders(self, open_only=True):
        return []

    def submit_order(self, intent):
        raise NotImplementedError

    def cancel_order(self, order_id):
        pass


def _settings(tmp_path) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "t.db",
                    strategies_dir=tmp_path / "strategies",
                    memory_dir=tmp_path / "memory")


def test_build_components_builds_both_accounts_with_distinct_brokers(tmp_path):
    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())

    assert set(components.accounts) == {"paper", "shadow"}
    # paper keeps the injected/Alpaca broker; shadow always gets its own
    # ShadowLedger -- broker_override never reaches the shadow bundle.
    assert isinstance(components.accounts["paper"].broker, FakeBroker)
    assert isinstance(components.accounts["shadow"].broker, ShadowLedger)
    assert components.accounts["paper"].broker is not components.accounts["shadow"].broker


def test_legacy_attribute_surface_points_at_paper(tmp_path):
    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())
    paper = components.accounts["paper"]

    assert components.broker is paper.broker
    assert components.journal is paper.journal
    assert components.gate is paper.gate
    assert components.executor is paper.executor
    assert components.queue is paper.queue
    assert components.strategies is paper.strategies
    assert components.memory is paper.memory
    assert components.reports is paper.reports
    assert components.observations is paper.observations
    assert components.sentinel is paper.sentinel
    # And it is genuinely paper, not shadow.
    assert components.broker is not components.accounts["shadow"].broker


def test_shadow_and_paper_strategy_dirs_are_separate(tmp_path):
    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())

    paper_dir = components.accounts["paper"].strategies.directory
    shadow_dir = components.accounts["shadow"].strategies.directory

    assert paper_dir != shadow_dir
    assert paper_dir.name == "paper"
    assert shadow_dir.name == "shadow"


def test_shadow_and_paper_memory_dirs_are_separate_but_share_profile(tmp_path):
    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())

    paper_memory = components.accounts["paper"].memory
    shadow_memory = components.accounts["shadow"].memory

    assert paper_memory.path_for("stock", "AAPL") != shadow_memory.path_for("stock", "AAPL")
    # Profile is deliberately shared -- same file regardless of account.
    assert paper_memory.path_for("profile", None) == shadow_memory.path_for("profile", None)


_STRAT_V1 = ("id: growth\nname: Growth\nstatus: active\nversion: 1\n"
            "position: {ticker: AAPL, target_weight: 10%}\nrules: []\n")
_STRAT_V2 = _STRAT_V1.replace("version: 1", "version: 2")


def test_approving_a_shadow_strategy_revision_writes_only_shadows_directory(tmp_path):
    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())
    shadow = components.accounts["shadow"]
    paper = components.accounts["paper"]

    (shadow.strategies.directory / "growth.yaml").write_text(_STRAT_V1)
    # Sanity: the same strategy id is free in paper's own directory.
    assert not (paper.strategies.directory / "growth.yaml").exists()

    handle = shadow.queue.add_strategy_revision(
        strategy_id="growth", ticker="AAPL", old_yaml=_STRAT_V1, new_yaml=_STRAT_V2,
        diff="version bump", rationale="test revision", source="chat", is_new=False)
    shadow.queue.approve(int(handle))

    assert "version: 2" in (shadow.strategies.directory / "growth.yaml").read_text()
    # Paper's directory was never touched by the shadow-scoped applier.
    assert not (paper.strategies.directory / "growth.yaml").exists()


def test_approving_a_paper_strategy_revision_never_touches_shadows_directory(tmp_path):
    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())
    shadow = components.accounts["shadow"]
    paper = components.accounts["paper"]

    (paper.strategies.directory / "growth.yaml").write_text(_STRAT_V1)
    assert not (shadow.strategies.directory / "growth.yaml").exists()

    handle = paper.queue.add_strategy_revision(
        strategy_id="growth", ticker="AAPL", old_yaml=_STRAT_V1, new_yaml=_STRAT_V2,
        diff="version bump", rationale="test revision", source="chat", is_new=False)
    paper.queue.approve(int(handle))

    assert "version: 2" in (paper.strategies.directory / "growth.yaml").read_text()
    assert not (shadow.strategies.directory / "growth.yaml").exists()


def test_each_account_sentinel_gets_its_own_drawdown_breaker(tmp_path):
    from allpath_trade.risk.breaker import DrawdownBreaker

    settings = _settings(tmp_path)
    components = build_components(settings, broker=FakeBroker())
    paper = components.accounts["paper"]
    shadow = components.accounts["shadow"]

    assert isinstance(paper.sentinel.breaker, DrawdownBreaker)
    assert isinstance(shadow.sentinel.breaker, DrawdownBreaker)
    assert paper.sentinel.breaker is not shadow.sentinel.breaker
    assert paper.sentinel.breaker.account == "paper"
    assert shadow.sentinel.breaker.account == "shadow"


def _settings_with_options(tmp_path, *, options_trading: bool,
                           alpaca_api_key: str = "", alpaca_secret_key: str = "") -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "t.db",
                    strategies_dir=tmp_path / "strategies",
                    memory_dir=tmp_path / "memory",
                    options_trading=options_trading,
                    alpaca_api_key=alpaca_api_key,
                    alpaca_secret_key=alpaca_secret_key)


def test_paper_executor_and_sentinel_share_one_options_backend_when_enabled(tmp_path):
    """Task 7: flag on + Alpaca keys present -- exactly one McpOptionsBackend
    is built for the paper account and handed to BOTH its Executor and its
    Sentinel (not two separate instances that would race two independent MCP
    sessions), while shadow -- never a real brokerage -- always gets None.
    Construction only, never a real call: McpOptionsBackend is lazy (see its
    own class docstring), so this asserts identity/type without spawning
    `uvx`."""
    from allpath_trade.broker.options_mcp import McpOptionsBackend

    settings = _settings_with_options(tmp_path, options_trading=True,
                                      alpaca_api_key="test-key",
                                      alpaca_secret_key="test-secret")
    components = build_components(settings, broker=FakeBroker())
    paper = components.accounts["paper"]
    shadow = components.accounts["shadow"]

    assert isinstance(paper.executor.options_backend, McpOptionsBackend)
    assert paper.executor.options_backend is paper.sentinel.options_backend
    assert shadow.executor.options_backend is None
    assert shadow.sentinel.options_backend is None


def test_options_backend_is_none_everywhere_when_flag_off(tmp_path):
    """Flag off (the default) -- construction is byte-identical to before
    this task: no McpOptionsBackend anywhere, for either account."""
    settings = _settings_with_options(tmp_path, options_trading=False,
                                      alpaca_api_key="test-key",
                                      alpaca_secret_key="test-secret")
    components = build_components(settings, broker=FakeBroker())
    paper = components.accounts["paper"]
    shadow = components.accounts["shadow"]

    assert paper.executor.options_backend is None
    assert paper.sentinel.options_backend is None
    assert shadow.executor.options_backend is None
    assert shadow.sentinel.options_backend is None


def test_options_backend_is_none_when_flag_on_but_no_alpaca_keys(tmp_path):
    """Flag on but no Alpaca keys configured (setup-wizard T1 territory) --
    there is nothing to build an MCP session against, so no backend either."""
    settings = _settings_with_options(tmp_path, options_trading=True,
                                      alpaca_api_key="", alpaca_secret_key="")
    components = build_components(settings, broker=FakeBroker())
    paper = components.accounts["paper"]

    assert paper.executor.options_backend is None
    assert paper.sentinel.options_backend is None
