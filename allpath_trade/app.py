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
from allpath_trade.memory.search import SessionSearch
from allpath_trade.memory.store import MemoryStore
from allpath_trade.migrate_files import migrate_files
from allpath_trade.notify.base import Notifier
from allpath_trade.notify.email import build_notifier
from allpath_trade.reflect import Reflector
from allpath_trade.risk.gate import RiskGate, RiskLimits
from allpath_trade.sentinel import Sentinel
from allpath_trade.store.accounts import ACCOUNTS, DEFAULT_ACCOUNT
from allpath_trade.store.app_state import AppState
from allpath_trade.store.conversations import ConversationStore
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal
from allpath_trade.store.llm_usage import LLMUsage
from allpath_trade.store.reports import ReportStore
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.store import StrategyStore


@dataclass
class AccountComponents:
    """Everything scoped to ONE account (`paper` or `shadow`) -- own broker,
    journal, risk gate + executor, review queue, strategies, conversations,
    reports, memory, observations, session search, sentinel, and (LLM
    permitting) consolidator/reflector. Shared, process-wide objects (the
    sqlite connection, the data source, the notifier, the LLM usage store,
    app_state, and MemoryStore's shared profile.md file) are NOT duplicated
    per account -- `conn`/`data` are carried here too as plain references
    (every account's stores/tools need them close at hand), everything else
    process-wide lives only on the parent `Components` container below.

    ChatService-compatible wiring (a per-account chat instance reachable
    from the web app / Telegram) is deliberately NOT built here -- wiring an
    account into the web/Telegram layer is Task 5's job; this task only
    builds what the sentinel and the nightly jobs (and Task 5's future
    ChatService) need in order to construct one, per spec §③."""

    account: str
    broker: Broker
    data: DataSource
    conn: sqlite3.Connection
    journal: TradeJournal
    gate: RiskGate
    executor: Executor
    queue: ReviewQueue
    strategies: StrategyStore
    conversations: ConversationStore
    reports: ReportStore
    memory: MemoryStore
    observations: ObservationLog
    search: SessionSearch
    sentinel: Sentinel
    consolidator: Consolidator | None = None
    reflector: Reflector | None = None


@dataclass
class Components:
    settings: Settings
    conn: sqlite3.Connection
    data: DataSource
    notifier: Notifier
    app_state: AppState
    llm_usage: LLMUsage
    accounts: dict[str, AccountComponents]

    # shadow-dual-active T4: LEGACY SINGLE-ACCOUNT ATTRIBUTE SURFACE.
    #
    # Every existing consumer of `Components` -- web routes, cli.py, the
    # Telegram poller/mirror -- was written against a single account and
    # reaches these attributes directly (`components.broker`,
    # `components.queue`, `components.sentinel`, ...). Rewiring every one
    # of those call sites onto `components.accounts[...]` (the account
    # switcher, per-account ChatService, row-bound approvals) is Task 5's
    # job, NOT this task's -- see the plan's Task 4 deliverable list.
    #
    # Until Task 5 lands, these fields are a plain alias for
    # `accounts[DEFAULT_ACCOUNT]`'s own objects, assigned once in
    # `build_components` right after `accounts` is built. Nothing here is
    # duplicated state: it is the SAME broker/journal/queue/... instance
    # `accounts["paper"]` already holds, just also reachable the old way,
    # so every existing route/command/test keeps behaving exactly as before
    # -- paper only -- while `shadow`'s bundle already exists alongside it
    # and runs its own sentinel pass + nightly chain (see scheduler.py).
    broker: Broker
    journal: TradeJournal
    gate: RiskGate
    executor: Executor
    queue: ReviewQueue
    strategies: StrategyStore
    memory: MemoryStore
    reports: ReportStore
    observations: ObservationLog
    sentinel: Sentinel

    # shadow-dual-active T5 addition to the legacy alias surface above:
    # `conversations`/`search`/`account` complete the set of fields
    # `AccountComponents` also carries, so `Components` is structurally a
    # drop-in stand-in for `accounts["paper"]` -- not just for the fields
    # that happened to already be here. This is what lets
    # `web/account_ctx.py`'s `bundle_for(components, "paper")` return
    # `components` itself rather than `components.accounts["paper"]` (a
    # DIFFERENT object, whose fields a test's `monkeypatch.setattr(holder.
    # get(), "broker", ...)` would never reach): every existing test that
    # monkeypatches an attribute directly on `holder.get()` -- and the whole
    # suite predates account-awareness, so none of them touch the cookie --
    # keeps working unchanged, because for the paper account (the default,
    # cookie-less view) "the current account's bundle" and "the shared
    # Components object" are the exact same object, by construction, not by
    # convention.
    conversations: ConversationStore
    search: SessionSearch
    consolidator: Consolidator | None = None
    reflector: Reflector | None = None
    account: str = DEFAULT_ACCOUNT


def _build_broker(account: str, settings: Settings, conn: sqlite3.Connection,
                  data: DataSource, broker_override: Broker | None) -> Broker:
    if account == DEFAULT_ACCOUNT:
        if broker_override is not None:
            return broker_override
        from allpath_trade.broker.alpaca import AlpacaBroker

        return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                            paper=settings.alpaca_paper)
    # shadow-dual-active T4: the shadow account is never Alpaca and never
    # takes `broker_override` (that parameter exists only for paper's own
    # tests/CLI injection, see build_components) -- it always gets its own
    # ShadowLedger, an in-DB bookkeeping ledger with no real-brokerage
    # credentials of any kind (broker/shadow.py's class docstring).
    from allpath_trade.broker.shadow import ShadowLedger

    return ShadowLedger(conn, data)


def _build_account_components(account: str, *, settings: Settings,
                              conn: sqlite3.Connection, data: DataSource,
                              notifier: Notifier, app_state: AppState,
                              llm_usage: LLMUsage,
                              broker_override: Broker | None) -> AccountComponents:
    broker = _build_broker(account, settings, conn, data, broker_override)
    journal = TradeJournal(conn, account=account)
    gate = RiskGate(RiskLimits())
    executor = Executor(broker, gate, journal, data)
    queue = ReviewQueue(conn, executor, account=account)
    # shadow-dual-active T4: use the classmethod so account validation gates
    # the directory resolution (StrategyStore.for_account's own docstring:
    # prevents a Task 5 shadow-bundle miss where the directory is resolved
    # but the account parameter itself was never validated).
    strategies = StrategyStore.for_account(settings.strategies_dir, conn,
                                           account=account)
    # Unconditional (unlike the LLM-backed wiring below): applying an
    # already-approved revision is plain file I/O, no LLM involved, so a
    # review approved via the web/CLI must work even when no LLM is
    # configured, for either account.
    queue.set_revision_applier(apply_revision_factory(strategies))
    if account == "shadow":
        # shadow-dual-active T6: the ledger-edit applier is only ever
        # meaningful on the shadow account (there is no ledger to edit on
        # paper) -- `ReviewQueue.add_shadow_edit` already refuses to queue a
        # row on any other account's instance, so leaving this unset for
        # paper is a second, structural belt on the same invariant. `broker`
        # here IS the ShadowLedger instance for this account (see
        # `_build_broker`); `conn` is the exact same connection object it was
        # constructed with, which `apply_shadow_edit_factory`'s docstring
        # depends on for its nested-transaction atomicity.
        from allpath_trade.agent.shadow_tools import apply_shadow_edit_factory

        queue.set_shadow_edit_applier(apply_shadow_edit_factory(broker, conn))
    conversations = ConversationStore(conn, account=account)
    reports = ReportStore(conn, account=account)
    memory = MemoryStore(settings.memory_dir, conn, account=account)
    # shadow-dual-active T4 CRITICAL carry (from T1's review): both
    # constructed with `account=` so a shadow turn/observation can never
    # surface in paper's session_search results, or vice versa -- see
    # ObservationLog's and SessionSearch's own docstrings.
    observations = ObservationLog(conn, account=account)
    search = SessionSearch(conn, account=account)
    sentinel = Sentinel(strategies, data, broker, executor, queue, notifier,
                       observations=observations, web_base_url=settings.web_base_url,
                       app_state=app_state, telegram_bot_token=settings.telegram_bot_token,
                       account=account)

    bundle = AccountComponents(
        account=account, broker=broker, data=data, conn=conn, journal=journal,
        gate=gate, executor=executor, queue=queue, strategies=strategies,
        conversations=conversations, reports=reports, memory=memory,
        observations=observations, search=search, sentinel=sentinel)

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
        bundle.consolidator = Consolidator(
            build_llm(settings, tier="memory", usage_store=llm_usage), memory,
            observations, journal, conn,
            conversations=conversations, app_state=app_state, account=account)
    except LLMConfigError:
        pass  # no LLM configured: Phase 2 behavior, same for either account

    if bundle.consolidator is not None:
        # Reflector needs the whole per-account component bundle (reports/
        # conn/conversations/journal/observations/search/broker/data/
        # strategies/queue/memory -- see its class docstring in reflect.py),
        # which only exists once `bundle` above has been assembled -- built
        # here, right after, rather than inside the try block above
        # alongside the consolidator. Gated on `bundle.consolidator is not
        # None` rather than repeating build_llm inside its own try/except:
        # the consolidator's own `build_llm(settings, tier="memory")` call a
        # few lines up already proved this exact (settings, tier) pair
        # doesn't raise LLMConfigError, so a second try/except here would be
        # dead code.
        from allpath_trade.llm.factory import build_llm

        bundle.reflector = Reflector(
            llm=build_llm(settings, tier="memory", usage_store=llm_usage),
            components=bundle, settings=settings, notifier=notifier)
    return bundle


def build_components(settings: Settings, broker: Broker | None = None,
                     conn: sqlite3.Connection | None = None) -> Components:
    # shadow-dual-active T2: one-shot, idempotent move of a legacy
    # single-account memory/strategies layout into memory/paper/ and
    # strategies/paper/ (backing up first if anything legacy is found).
    # Must run before any store below is constructed, and before this
    # function's own strategies_dir/memory_dir use just below -- both
    # already assume the post-migration (per-account) shape. Idempotent, so
    # this is safe to call unconditionally even though cli.py's `main` also
    # calls it before dispatching to any command (cli.py's broker-less
    # commands -- "strategies", "rearm", "reviews list/reject", "memory
    # show" -- never reach this function at all, so they need their own
    # call; this one covers every OTHER caller of build_components, e.g.
    # the web app's ComponentHolder).
    migrate_files(settings)

    if conn is None:
        conn = connect(settings.db_path)
    data = YFinanceSource()
    notifier = build_notifier(settings)
    app_state = AppState(conn)
    llm_usage = LLMUsage(conn)

    # shadow-dual-active T4: both accounts always active -- own broker,
    # journal, queue, strategies, memory, sentinel, and (LLM permitting)
    # consolidator/reflector -- sharing only the conn/data/notifier/
    # llm_usage/app_state built above (and MemoryStore's shared profile.md
    # file, which comes free: every MemoryStore's `path_for("profile", ...)`
    # resolves to the same root-level file regardless of `account`). `broker`
    # (the test-injection / cli.py hook) only ever stands in for paper's own
    # broker -- see `_build_broker`.
    accounts: dict[str, AccountComponents] = {
        account: _build_account_components(
            account, settings=settings, conn=conn, data=data, notifier=notifier,
            app_state=app_state, llm_usage=llm_usage,
            broker_override=broker if account == DEFAULT_ACCOUNT else None)
        for account in ACCOUNTS
    }

    paper = accounts[DEFAULT_ACCOUNT]
    return Components(
        settings=settings, conn=conn, data=data, notifier=notifier,
        app_state=app_state, llm_usage=llm_usage, accounts=accounts,
        broker=paper.broker, journal=paper.journal, gate=paper.gate,
        executor=paper.executor, queue=paper.queue, strategies=paper.strategies,
        memory=paper.memory, reports=paper.reports, observations=paper.observations,
        sentinel=paper.sentinel, consolidator=paper.consolidator,
        reflector=paper.reflector, conversations=paper.conversations,
        search=paper.search, account=paper.account)
