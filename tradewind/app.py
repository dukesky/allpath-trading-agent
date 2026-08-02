from __future__ import annotations

from dataclasses import dataclass

from tradewind.broker.base import Broker
from tradewind.config import Settings
from tradewind.data.base import DataSource
from tradewind.data.yf import YFinanceSource
from tradewind.execution import Executor
from tradewind.notify.base import Notifier
from tradewind.notify.email import build_notifier
from tradewind.risk.gate import RiskGate, RiskLimits
from tradewind.sentinel import Sentinel
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal
from tradewind.store.reviews import ReviewQueue
from tradewind.strategy.store import StrategyStore


@dataclass
class Components:
    settings: Settings
    broker: Broker
    data: DataSource
    journal: TradeJournal
    gate: RiskGate
    executor: Executor
    queue: ReviewQueue
    strategies: StrategyStore
    notifier: Notifier
    sentinel: Sentinel


def build_components(settings: Settings, broker: Broker | None = None) -> Components:
    if broker is None:
        from tradewind.broker.alpaca import AlpacaBroker

        broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                              paper=settings.alpaca_paper)
    conn = connect(settings.db_path)
    data = YFinanceSource()
    journal = TradeJournal(conn)
    gate = RiskGate(RiskLimits())
    executor = Executor(broker, gate, journal, data)
    queue = ReviewQueue(conn, executor)
    settings.strategies_dir.mkdir(parents=True, exist_ok=True)
    strategies = StrategyStore(settings.strategies_dir, conn)
    notifier = build_notifier(settings)
    sentinel = Sentinel(strategies, data, broker, executor, queue, notifier)
    try:
        from tradewind.agent.readonly_tools import register_readonly_tools
        from tradewind.agent.review import ReviewAgent
        from tradewind.agent.tools import ToolRegistry
        from tradewind.llm.factory import LLMConfigError, build_llm

        review_llm = build_llm(settings, tier="review")
        review_registry = ToolRegistry()
        register_readonly_tools(review_registry, data=data, broker=broker,
                                journal=journal, strategies=strategies,
                                queue=queue)
        sentinel.review_agent = ReviewAgent(review_llm, review_registry)
    except LLMConfigError:
        pass  # no LLM configured: Phase 2 behavior
    return Components(settings=settings, broker=broker, data=data, journal=journal,
                      gate=gate, executor=executor, queue=queue,
                      strategies=strategies, notifier=notifier, sentinel=sentinel)
