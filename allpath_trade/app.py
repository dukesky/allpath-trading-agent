from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from allpath_trade.agent.reflection_tools import apply_revision_factory
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
from allpath_trade.reflect import Reflector
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.sentinel import Sentinel
from allpath_trade.store.app_state import AppState
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.llm_usage import LLMUsage
from allpath_trade.store.reports import ReportStore
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
    app_state: AppState
    reports: ReportStore
    llm_usage: LLMUsage
    consolidator: Consolidator | None = None
    reflector: Reflector | None = None


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
    # Unconditional (unlike the LLM-backed wiring in the try/except below):
    # applying an already-approved revision is plain file I/O, no LLM
    # involved, so a review approved via the web/CLI must work even when no
    # LLM is configured.
    queue.set_revision_applier(apply_revision_factory(strategies))
    notifier = build_notifier(settings)
    observations = ObservationLog(conn)
    memory = MemoryStore(settings.memory_dir, conn)
    app_state = AppState(conn)
    reports = ReportStore(conn)
    llm_usage = LLMUsage(conn)
    sentinel = Sentinel(strategies, data, broker, executor, queue, notifier,
                       observations=observations, web_base_url=settings.web_base_url,
                       app_state=app_state, telegram_bot_token=settings.telegram_bot_token)
    consolidator: Consolidator | None = None
    try:
        from allpath_trade.agent.readonly_tools import register_readonly_tools
        from allpath_trade.agent.review import ReviewAgent
        from allpath_trade.agent.tools import ToolRegistry
        from allpath_trade.llm.factory import LLMConfigError, build_llm

        review_llm = build_llm(settings, tier="review", usage_store=llm_usage)
        review_registry = ToolRegistry()
        register_readonly_tools(review_registry, data=data, broker=broker,
                                journal=journal, strategies=strategies,
                                queue=queue)
        sentinel.review_agent = ReviewAgent(review_llm, review_registry, memory=memory)
        consolidator = Consolidator(
            build_llm(settings, tier="memory", usage_store=llm_usage), memory,
            observations, journal, conn,
            conversations=ConversationStore(conn),
            app_state=app_state)
    except LLMConfigError:
        pass  # no LLM configured: Phase 2 behavior

    components = Components(
        settings=settings, broker=broker, data=data, journal=journal,
        gate=gate, executor=executor, queue=queue,
        strategies=strategies, notifier=notifier, sentinel=sentinel,
        conn=conn, observations=observations, memory=memory,
        app_state=app_state, reports=reports, llm_usage=llm_usage,
        consolidator=consolidator)

    if consolidator is not None:
        # Reflector needs the whole component bag (reports/conn/journal/
        # observations/broker/data/strategies/queue/memory -- see its class
        # docstring in reflect.py), which only exists once `components`
        # above has been assembled -- built here, right after, rather than
        # inside the try block above alongside the consolidator. Gated on
        # `consolidator is not None` rather than repeating build_llm inside
        # its own try/except: the consolidator's own
        # `build_llm(settings, tier="memory")` call a few lines up already
        # proved this exact (settings, tier) pair doesn't raise
        # LLMConfigError, so a second try/except here would be dead code.
        from allpath_trade.llm.factory import build_llm

        components.reflector = Reflector(
            llm=build_llm(settings, tier="memory", usage_store=llm_usage),
            components=components, settings=settings, notifier=notifier)
    return components
