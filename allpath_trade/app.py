from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings
from allpath_trade.data.base import DataSource
from allpath_trade.data.yf import YFinanceSource
from allpath_trade.execution import Executor
from allpath_trade.memory.consolidate import Consolidator
from allpath_trade.memory.observations import ObservationLog
from allpath_trade.memory.store import MemoryStore
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.email import build_notifier
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.sentinel import Sentinel
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore


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
    conn: sqlite3.Connection
    observations: ObservationLog
    memory: MemoryStore
    consolidator: Consolidator | None = None


def build_components(settings: Settings, broker: Broker | None = None,
                     conn: sqlite3.Connection | None = None) -> Components:
    if broker is None:
        from allpath_trade.broker.alpaca import AlpacaBroker

        broker = AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                              paper=settings.alpaca_paper)
    if conn is None:
        conn = connect(settings.db_path)
    data = YFinanceSource()
    journal = TradeJournal(conn)
    gate = RiskGate(RiskLimits())
    executor = Executor(broker, gate, journal, data)
    queue = ReviewQueue(conn, executor)
    settings.strategies_dir.mkdir(parents=True, exist_ok=True)
    strategies = StrategyStore(settings.strategies_dir, conn)
    notifier = build_notifier(settings)
    observations = ObservationLog(conn)
    memory = MemoryStore(settings.memory_dir, conn)
    sentinel = Sentinel(strategies, data, broker, executor, queue, notifier,
                       observations=observations)
    consolidator: Consolidator | None = None
    try:
        from allpath_trade.agent.readonly_tools import register_readonly_tools
        from allpath_trade.agent.review import ReviewAgent
        from allpath_trade.agent.tools import ToolRegistry
        from allpath_trade.llm.factory import LLMConfigError, build_llm

        review_llm = build_llm(settings, tier="review")
        review_registry = ToolRegistry()
        register_readonly_tools(review_registry, data=data, broker=broker,
                                journal=journal, strategies=strategies,
                                queue=queue)
        sentinel.review_agent = ReviewAgent(review_llm, review_registry, memory=memory)
        consolidator = Consolidator(build_llm(settings, tier="memory"), memory,
                                    observations, journal, conn)
    except LLMConfigError:
        pass  # no LLM configured: Phase 2 behavior
    return Components(settings=settings, broker=broker, data=data, journal=journal,
                      gate=gate, executor=executor, queue=queue,
                      strategies=strategies, notifier=notifier, sentinel=sentinel,
                      conn=conn, observations=observations, memory=memory,
                      consolidator=consolidator)
