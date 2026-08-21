from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from functools import partial

from pydantic import ValidationError

from allpath_trade.agent.readonly_tools import _format_recent_trade
from allpath_trade.agent.reflection_tools import apply_revision_factory
from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings, SettingsStore, describe_validation_error
from allpath_trade.llm.base import LLMClient
from allpath_trade.migrate_files import migrate_files
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


def _default_broker(settings: Settings) -> Broker:
    from allpath_trade.broker.alpaca import AlpacaBroker

    return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                        paper=settings.alpaca_paper)


def cmd_status(settings: Settings, broker: Broker, account: str = "paper") -> int:
    try:
        acct = broker.get_account()
        mode = "PAPER" if broker.is_paper else "LIVE"
        print(f"[{broker.name} / {mode.lower()}]")
        print(f"equity: {acct.equity}  cash: {acct.cash}  buying_power: {acct.buying_power}")
        positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001 - CLI boundary: report and exit, never crash
        print(f"Could not reach broker: {exc}. Check your Alpaca keys in .env.",
              file=sys.stderr)
        return 1

    if positions:
        print("\npositions:")
        for p in positions:
            print(f"  {p.ticker:6} qty={p.qty} avg={p.avg_entry_price} "
                  f"value={p.market_value} pl={p.unrealized_pl}")
    else:
        print("\nno open positions")

    # shadow-dual-active T5: `account` (default "paper", matching this
    # function's pre-T5 hardcoded behavior exactly) scopes the journal read
    # to the account `status` was invoked for -- otherwise `--account
    # shadow status` would print paper's trade history under a shadow
    # broker summary.
    journal = TradeJournal(connect(settings.db_path), account=account)
    rows = journal.recent(limit=5)
    if rows:
        print("\nrecent trades:")
        # I1: reuse the same formatter as get_portfolio/build_system_prompt
        # (agent/readonly_tools._format_recent_trade) so `status` prints the
        # same "submitted"-labeled, honesty-checked line everywhere instead
        # of a fourth independent (and previously bare, unlabeled-timestamp)
        # copy.
        for r in rows:
            print(f"  #{r['id']} {_format_recent_trade(r)}")
    return 0


def cmd_serve(settings: Settings, host: str | None, port: int | None) -> int:
    import uvicorn

    from allpath_trade.web.app import create_app
    from allpath_trade.web.auth import ensure_token

    host = host or settings.web_host
    port = port or settings.web_port

    # Must run before create_app: `ensure_token` mutates `settings.web_token`
    # in place, and `create_app` hands that same `Settings` instance down
    # through `build_components`/`ComponentHolder`. Calling it first only
    # works today because of that by-reference aliasing; calling it before
    # construction means first-run auth doesn't depend on that fact holding.
    had_token = bool(settings.web_token)
    token = ensure_token(SettingsStore(), settings)
    app = create_app(settings, start_scheduler=True)

    shown = "localhost" if host in {"127.0.0.1", "localhost"} else host
    print(f"[allpath-trade] http://{shown}:{port}")
    # The token is the session cookie's value -- only worth printing (into
    # terminal scrollback and any log capture) the moment it's generated.
    # On later starts, point at where it already lives instead of repeating it.
    if had_token:
        print("[allpath-trade] access token: unchanged (see WEB_TOKEN in .env)")
    else:
        print(f"[allpath-trade] access token: {token}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def cmd_check(sentinel) -> int:
    report = sentinel.run_once()
    print(f"strategies checked: {report.strategies_checked}")
    for o in report.outcomes:
        print(f"  {o.strategy_id}/{o.rule_id}: {o.disposition}"
              + (f" ({o.detail})" if o.detail else ""))
    for e in report.errors:
        print(f"  error: {e}", file=sys.stderr)
    return 1 if report.errors else 0


def cmd_strategies(settings: Settings, store) -> int:
    errors: list[str] = []
    docs = store.load_all(status=None, errors=errors)
    for err in errors:
        print(f"warning: {err}", file=sys.stderr)
    if not docs:
        # store.directory (not settings.strategies_dir) -- shadow-dual-
        # active T2 moved the actual scanned directory to
        # strategies/{account}/, so this must name what `load_all` above
        # actually globbed, not the un-account-scoped settings root.
        print("no strategies found in", store.directory)
        return 0
    for d in docs:
        print(f"{d.id}  [{d.status.value}/{d.authorization.value}]  {d.name}")
        for r in d.rules:
            print(f"    {r.id} ({r.type.value}, {r.state.value}): "
                  f"{r.condition} -> {r.action}")
    return 0


def cmd_rearm(store, strategy_id: str, rule_id: str) -> int:
    from allpath_trade.strategy.loader import StrategyValidationError

    try:
        doc = store.load(strategy_id)
    except FileNotFoundError:
        print(f"strategy '{strategy_id}' not found", file=sys.stderr)
        return 1
    except StrategyValidationError as exc:
        print(f"strategy '{strategy_id}' is invalid: {exc}", file=sys.stderr)
        return 1
    if rule_id not in {r.id for r in doc.rules}:
        print(f"rule {rule_id} not found in {strategy_id}", file=sys.stderr)
        return 1
    store.rearm(strategy_id, rule_id)
    print(f"{strategy_id}/{rule_id} re-armed")
    return 0


def _approve_needs_broker(settings: Settings, review_id: int, account: str) -> bool:
    """`reviews approve` on a strategy_revision row is pure file I/O -- no
    broker/executor is ever touched (see ReviewQueue._approve_revision:
    strategy_revision rows never reach `_executor`). Finding 6: the old
    blanket `needs_broker` for every `reviews approve` call demanded Alpaca
    credentials even for those rows. Peeks the row's `kind` directly with a
    throwaway connection -- deliberately not through ReviewQueue/
    build_components, which is exactly the broker-requiring machinery this
    is trying to avoid entering. Missing/unreadable rows keep the old
    conservative default (broker required) so `cmd_reviews` still gets to
    report its own "review not found" error rather than this silently
    swallowing it.

    shadow-dual-active T5 carry (from T1's review): the query now also
    filters on `account` -- `cmd_reviews`'s own `queue` is built scoped to
    `--account` (default paper) below, so an id that exists but belongs to
    the OTHER account must read the same as "not found" here too (that
    queue's own `.get`/`.approve` already treat it that way -- see
    ReviewQueue.get's docstring), not accidentally match a stray row and
    make the wrong claim about whether a broker is needed."""
    # F6: this throwaway connection was never closed -- every `reviews
    # approve` invocation (the common case, not just this function's own
    # narrow strategy_revision check) leaked one sqlite connection for the
    # life of the process. try/finally rather than a `with` block: the
    # LockedConnection connect() returns doesn't implement the context
    # manager protocol (see store/db.py) -- only .close().
    conn = connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT kind FROM pending_reviews WHERE id = ? AND account = ?",
            (review_id, account)).fetchone()
        return row is None or row["kind"] != "strategy_revision"
    finally:
        conn.close()


def cmd_reviews(q, args, store=None) -> int:
    from allpath_trade.store.reviews import ReviewError, RevisionValidationError

    try:
        if args.reviews_command == "list":
            rows = q.list()
            if not rows:
                print("no pending reviews")
            for r in rows:
                print(f"#{r['id']} {r['ts'][:19]} {r['strategy_id']}/{r['rule_id']} "
                      f"[{r['status']}] {r['condition']} -> {r['action']}")
        elif args.reviews_command == "approve":
            # kind-branch HARD PREREQ (Task 3): fetch the row and read its
            # kind BEFORE calling approve() -- approve() returns None for
            # strategy_revision rows (Task 2), so `result.submitted` below
            # would crash on those if this branch reads `result` first.
            row = q.get(args.review_id)
            kind, strategy_id = row["kind"], row["strategy_id"]
            try:
                result = q.approve(args.review_id)
            except RevisionValidationError as exc:
                # ReviewQueue already rolled the row back to "pending"
                # before raising -- say that, not just "error".
                print(f"error: revision failed re-validation and was left "
                      f"pending: {exc}", file=sys.stderr)
                return 1
            if kind == "strategy_revision":
                # Finding F2: mirror the web approve flow's warning -- see
                # StrategyStore.rearm_warning's docstring for why this never
                # re-arms anything on its own. `store` is None only in
                # cmd_reviews' own tests that construct a bare queue; every
                # real caller (main(), below) passes the StrategyStore.
                note = store.rearm_warning(strategy_id) if store is not None else ""
                print(f"Revision applied to {strategy_id}.{note}")
            else:
                print("executed" if result.submitted
                      else f"rejected by risk gate: {'; '.join(result.decision.reasons)}")
        else:
            q.reject(args.review_id, note=args.note or "")
            print(f"review {args.review_id} rejected")
        return 0
    except ReviewError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_memory_show(memory, layer: str | None, key: str | None) -> int:
    if layer:
        from allpath_trade.memory.store import MemoryStoreError

        try:
            text = memory.read(layer, key)
        except MemoryStoreError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(text if text.strip() else "(empty)")
        return 0
    root = memory.root
    files = sorted(root.rglob("*.md")) if root.exists() else []
    if not files:
        print("no memory files yet — the agent writes them as you work together")
        return 0
    for f in files:
        print(f"{f.relative_to(root)}  ({f.stat().st_size} bytes)")
    return 0


CHAT_BANNER = r"""
          |\
          | \       a l l p a t h   t r a d e
          |  \      your mid/long-term investing copilot
       ___|___\__
       \_________/
    ~ ~ ~ ~ ~ ~ ~ ~
"""


def _cli_chat_bundle(components, account: str):
    """See the call site's comment (`main`'s "chat" branch) for why this
    exists: `cmd_chat` below reads account-scoped stores AND shared
    settings/llm_usage off one object. For paper this is `components`
    itself (unchanged pre-T5 behavior, zero risk to the existing terminal
    chat test suite, which never passes `--account`); for shadow it's a
    thin `SimpleNamespace` bridging `components.accounts["shadow"]`'s own
    stores with `components`'s shared settings/llm_usage -- built fresh on
    every call rather than cached anywhere, since it's cheap and only ever
    used once per `cli chat` invocation."""
    from types import SimpleNamespace

    from allpath_trade.store.accounts import DEFAULT_ACCOUNT

    if account == DEFAULT_ACCOUNT:
        return components
    b = components.accounts[account]
    return SimpleNamespace(
        conn=b.conn, data=b.data, broker=b.broker, journal=b.journal,
        strategies=b.strategies, queue=b.queue, executor=b.executor,
        memory=b.memory, consolidator=b.consolidator,
        settings=components.settings, llm_usage=components.llm_usage)


def cmd_chat(components, llm, *, new: bool, input_fn=None, memory_llm=None) -> int:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    from allpath_trade.agent.action_tools import register_action_tools
    from allpath_trade.agent.compact import Compactor
    from allpath_trade.agent.context import build_system_prompt, load_identity
    from allpath_trade.agent.loop import AgentSession
    from allpath_trade.agent.memory_tools import register_memory_tools
    from allpath_trade.agent.readonly_tools import register_readonly_tools
    from allpath_trade.agent.tools import ToolRegistry
    from allpath_trade.memory.search import SessionSearch
    from allpath_trade.store.conversations import ConversationStore

    # Resolved at call time (not as a default-arg value) so tests can
    # monkeypatch builtins.input and have it take effect here.
    if input_fn is None:
        input_fn = input

    console = Console(highlight=False)
    store = ConversationStore(components.conn)
    cid = store.start() if new or store.latest() is None else store.latest()

    def confirm(prompt: str) -> bool:
        console.print(Panel(prompt, title="confirmation required",
                            border_style="yellow"))
        return input_fn("Confirm? [y/N] ").strip().lower() in ("y", "yes")

    def on_tool(call) -> None:
        args = ", ".join(f"{k}={v!r}" for k, v in call.arguments.items())
        console.print(f"  [dim]⚙ {call.name}({args})[/dim]")

    registry = ToolRegistry()
    register_readonly_tools(registry, data=components.data,
                            broker=components.broker, journal=components.journal,
                            strategies=components.strategies,
                            queue=components.queue)
    register_action_tools(registry, strategies=components.strategies,
                          executor=components.executor, confirm=confirm)
    register_memory_tools(registry, memory=components.memory,
                          search=SessionSearch(components.conn))
    system = build_system_prompt(identity=load_identity(),
                                 broker=components.broker,
                                 journal=components.journal,
                                 strategies=components.strategies,
                                 queue=components.queue,
                                 memory=components.memory)
    # The terminal chat resumes the same unbounded conversation the web chat
    # does (allpath_trade/web/chat_service.py ChatService._build) -- without
    # a Compactor here too, a long-lived interactive session grows its
    # context forever in exactly the same way. `memory_llm` is normally
    # supplied by main() (built through the same injectable factory as the
    # chat-tier `llm`, so tests can script it); build it directly here only
    # for callers that construct cmd_chat's dependencies themselves.
    if memory_llm is None:
        from allpath_trade.llm.factory import build_llm

        memory_llm = build_llm(components.settings, tier="memory",
                              usage_store=components.llm_usage)
    # Finding 8: flush durable preferences to curated memory before the
    # older half of the conversation is dropped from context -- see
    # Compactor's docstring on on_before_compact. Without this the only
    # backstop is _finish()'s end-of-session consolidation below, which
    # never runs until the user exits; a preference stated once early in a
    # long session could be compacted away long before that. No-op when no
    # LLM is configured (components.consolidator is None in that case).
    # F2: `propagate=True` -- Compactor's own try/except is what turns a
    # failed flush into "skip compaction this round, keep the messages",
    # but only if the hook actually raises. run_post_chat's default swallows
    # every failure into a string return, which reads to Compactor as an
    # ordinary success.
    compactor = Compactor(
        memory_llm, store, budget_tokens=components.settings.context_budget_tokens,
        on_before_compact=(
            partial(components.consolidator.run_post_chat, propagate=True)
            if components.consolidator is not None else None))
    session = AgentSession(llm, registry, system, store=store,
                           conversation_id=cid, on_tool=on_tool,
                           compactor=compactor)
    # Finding 6: a plain list index (`len(session.history)`) breaks the
    # moment a compaction runs mid-session -- run_turn (loop.py) reassigns
    # `session.history` to the trimmed tail maybe_compact returns, so its
    # length can drop below whatever was captured here before any
    # compaction happened, especially when resuming a conversation that
    # already had history: `session.history[initial_len:]` then silently
    # yields `[]` (or the wrong window) right when there's the most to
    # remember. Turn ids are monotonic and the store keeps the full
    # transcript regardless of what compaction trims from context (see
    # compact.py's class docstring), so anchoring on the last stored turn id
    # instead of a list length survives any number of compactions.
    existing_ids = [tid for tid, _ in store.history_with_ids(cid)]
    start_turn_id = existing_ids[-1] if existing_ids else 0

    mode = "paper" if components.broker.is_paper else "LIVE"
    console.print(f"[bold cyan]{CHAT_BANNER}[/bold cyan]")
    console.print(f"  [dim]model[/dim]  [bold]{llm.model}[/bold] "
                  f"[dim]via {components.settings.llm_provider} · {mode} account[/dim]")
    console.print(f"  [dim]chat[/dim]   conversation #{cid} · /exit to quit · "
                  "orders & strategy changes always ask first\n")

    def _finish() -> None:
        # Finding 7: consolidate_after_chat is a real Settings field and a
        # live checkbox on the settings page, but until now nothing read it
        # -- this ran unconditionally regardless of what the user set.
        if (components.consolidator is not None
                and components.settings.consolidate_after_chat):
            new_msgs = store.history(cid, after_turn_id=start_turn_id)
            if new_msgs:
                try:
                    note = components.consolidator.run_post_chat(new_msgs)
                    console.print(f"[dim]memory: {note}[/dim]")
                except Exception:  # noqa: BLE001, S110 — exit must never fail
                    pass

    # Colored input prompt only on a real terminal (tests/pipes get plain text).
    prompt = "\x1b[1;36myou ▸ \x1b[0m" if sys.stdout.isatty() else "you ▸ "
    interrupted_once = False
    while True:
        try:
            user = input_fn(prompt)
        except EOFError:
            console.print()
            _finish()
            return 0
        except KeyboardInterrupt:
            if interrupted_once:
                console.print()
                _finish()
                console.print("[dim]bye — the sentinel keeps watching your rules.[/dim]")
                return 0
            interrupted_once = True
            console.print("\n[dim](ctrl+c again to exit, or keep typing)[/dim]")
            continue
        interrupted_once = False
        if user.strip() in ("/exit", "/quit"):
            _finish()
            console.print("[dim]bye — the sentinel keeps watching your rules.[/dim]")
            return 0
        if not user.strip():
            continue
        try:
            reply = session.run_turn(user)
        except KeyboardInterrupt:
            console.print("\n[dim](interrupted — the reply was discarded)[/dim]")
            continue
        console.print("\n[bold magenta]◆ allpath[/bold magenta]")
        console.print(Markdown(reply or "(no reply)"))
        console.print()


CLI_DESCRIPTION = """\
allpath-trade — a self-hosted, LLM-powered mid/long-term trading agent.

typical workflow:
  1. allpath-trade status              check your (paper) brokerage connection
  2. allpath-trade chat                talk to the agent, co-create strategies
  3. allpath-trade strategies          see what the sentinel is watching
  4. allpath-trade run                 start the hourly monitoring daemon
  5. allpath-trade reviews list        act on triggers awaiting your approval
"""

CLI_EPILOG = """\
examples:
  allpath-trade chat --new                       start a fresh conversation
  allpath-trade rearm aapl-long stop-loss        re-arm a triggered rule
  allpath-trade reviews approve 3                execute pending review #3
  allpath-trade memory show --layer stock --key AAPL
                                             read the agent's AAPL dossier

keys live in .env (see .env.example). Read-only commands (strategies, rearm,
reviews list/reject, memory show) work without any keys.
"""


def _add_account_arg(p: argparse.ArgumentParser) -> None:
    """shadow-dual-active T5: `--account paper|shadow` on every command
    that's scoped to one account (default paper, matching every other
    account-less boundary in this app -- store/accounts.DEFAULT_ACCOUNT).
    `run`/`serve` deliberately do NOT get this: both already run BOTH
    accounts' pipelines at once (scheduler.py's `run_daemon`/`build_jobs`),
    so there is no single account for a flag to select there."""
    from allpath_trade.store.accounts import ACCOUNTS, DEFAULT_ACCOUNT

    p.add_argument("--account", choices=ACCOUNTS, default=DEFAULT_ACCOUNT,
                   help=f"which account to operate on (default: {DEFAULT_ACCOUNT})")


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None,
         llm_factory: Callable[[Settings, str], LLMClient] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="allpath-trade", description=CLI_DESCRIPTION, epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="<command>")
    p_status = sub.add_parser(
        "status", help="show account, positions, recent trades",
        description="Show brokerage account (equity/cash/buying power), open "
                    "positions, and the last 5 journaled trades. "
                    "Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env.")
    _add_account_arg(p_status)
    p_check = sub.add_parser(
        "check", help="run one sentinel pass now",
        description="Evaluate every armed rule of every active strategy once, "
                    "right now — useful for testing a strategy you just wrote. "
                    "Hard rules on auto-strategies may EXECUTE (paper) trades.")
    _add_account_arg(p_check)
    sub.add_parser(
        "run", help="run the monitoring daemon",
        description="Long-running daemon: sentinel pass every "
                    "SENTINEL_INTERVAL_MINUTES (default 60) during US market "
                    "hours, plus the daily memory consolidation after close. "
                    "Runs BOTH accounts -- no --account flag, see 'serve'.")
    p_strategies = sub.add_parser(
        "strategies", help="list strategies and rule states",
        description="List every strategy YAML in strategies/{account}/ with "
                    "its status, authorization level, and each rule's "
                    "armed/triggered state. Works without any keys.")
    _add_account_arg(p_strategies)
    p_rearm = sub.add_parser(
        "rearm", help="re-arm a triggered rule",
        description="Rules fire once, then stay 'triggered' until re-armed. "
                    "Example: allpath-trade rearm aapl-long stop-loss")
    p_rearm.add_argument("strategy_id", help="strategy file name without .yaml")
    p_rearm.add_argument("rule_id", help="rule id inside the strategy")
    _add_account_arg(p_rearm)
    p_reviews = sub.add_parser(
        "reviews", help="approve/reject triggers awaiting review",
        description="Soft-rule triggers (and everything on confirm-level "
                    "strategies) queue here with the agent's analysis attached. "
                    "Approving executes through the risk gate.")
    _add_account_arg(p_reviews)
    rsub = p_reviews.add_subparsers(dest="reviews_command", required=True,
                                    metavar="<action>")
    rsub.add_parser("list", help="list pending reviews")
    p_app = rsub.add_parser("approve", help="execute a pending review (needs keys)")
    p_app.add_argument("review_id", type=int, help="id from 'reviews list'")
    p_rej = rsub.add_parser("reject", help="dismiss a pending review")
    p_rej.add_argument("review_id", type=int, help="id from 'reviews list'")
    p_rej.add_argument("--note", default="", help="reason, stored for the record")
    p_chat = sub.add_parser(
        "chat", help="talk to the agent",
        description="Interactive chat with the trading agent: discuss stocks, "
                    "research with live data + web search, draft strategies, "
                    "propose orders. Every order and strategy change asks for "
                    "your y/N confirmation. Requires Alpaca + LLM keys in .env. "
                    "Exit with /exit, ctrl+d, or ctrl+c twice.")
    p_chat.add_argument("--new", action="store_true",
                        help="start a new conversation instead of resuming the last")
    _add_account_arg(p_chat)
    p_mem = sub.add_parser(
        "memory", help="inspect or consolidate the agent's memory",
        description="The agent's curated memory lives in memory/ as plain "
                    "markdown you can read and edit. Layers: profile, strategy, "
                    "stock, lesson.")
    _add_account_arg(p_mem)
    msub = p_mem.add_subparsers(dest="memory_command", required=True,
                                metavar="<action>")
    m_show = msub.add_parser(
        "show", help="list memory files, or print one layer",
        description="Without arguments: list all memory files with sizes. "
                    "With --layer (and --key for strategy/stock/lesson): print "
                    "that file. Works without any keys.")
    m_show.add_argument("--layer", default=None,
                        help="profile | strategy | stock | lesson")
    m_show.add_argument("--key", default=None,
                        help="strategy id / ticker / lesson slug")
    msub.add_parser(
        "consolidate", help="distill recent events into memory now (needs LLM key)",
        description="Manually run the daily consolidation pass: recent trades, "
                    "triggers, and observations are distilled into the curated "
                    "memory layers. Normally runs automatically after close.")
    p_serve = sub.add_parser(
        "serve", help="run the web interface and sentinel",
        description="Run the FastAPI web UI and the sentinel scheduler in one "
                    "process. Requires Alpaca keys in .env. Serves BOTH "
                    "accounts -- no --account flag, the web UI's own "
                    "switcher picks the account per browser session.")
    p_serve.add_argument("--host", default=None, help="bind address")
    p_serve.add_argument("--port", type=int, default=None, help="port")

    if argv is not None and len(argv) == 0:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    try:
        settings = SettingsStore().load()
    except ValidationError as exc:
        # F1: the range constraints Finding 4 put on Settings (sentinel
        # interval >= 1, context budget >= 2000, smtp port in range) reject a
        # value that was legal before this branch. Without this guard, an
        # existing .env carrying such a value takes down every single
        # command with a raw traceback -- including the settings page that
        # could otherwise fix it, since `serve` loads settings the same way.
        # Same friendly-error-and-exit-2 shape as the missing-credentials
        # check just below, for the same reason: a config problem is the
        # user's to fix in .env, not a crash to report.
        print(f"Invalid setting in .env: {describe_validation_error(exc)}. "
              "Fix or remove that line in .env, then try again.", file=sys.stderr)
        return 2

    # shadow-dual-active T2: every CLI command that touches strategies/ or
    # memory/ must see the post-migration (per-account) layout, not just the
    # ones that happen to route through `build_components` below (that
    # covers "status"/"check"/"run"/"chat"/"serve" and revision-kind
    # "reviews approve" -- but "strategies", "rearm", plain "reviews
    # list"/"reject", and "memory show" all construct their stores directly,
    # with no broker and no build_components call, and must be just as
    # correct against a legacy on-disk layout). Idempotent, so calling it
    # again inside `build_components` a few lines down (that call exists for
    # every OTHER caller of build_components, e.g. the web app) is a no-op,
    # not a double-migration.
    migrate_files(settings)

    # shadow-dual-active T5: which account THIS invocation is scoped to.
    # `getattr` with a default rather than `args.account` directly -- "run"
    # and "serve" never get the `--account` flag at all (see
    # `_add_account_arg`'s docstring: both already run every account), so
    # `args` has no such attribute for those two commands.
    from allpath_trade.store.accounts import DEFAULT_ACCOUNT

    account = getattr(args, "account", DEFAULT_ACCOUNT)

    # Only commands that actually reach the broker require credentials;
    # read-only commands (strategies, rearm, reviews list/reject) work
    # without. `reviews approve` only needs one when the row being approved
    # is order-kind -- a strategy_revision approval is pure file I/O (see
    # `_approve_needs_broker`).
    #
    # shadow-dual-active T5 note: this stays UNCHANGED from before this task
    # (no `account`-conditional relaxation for shadow) even though shadow's
    # own ShadowLedger broker never touches Alpaca -- `build_components`
    # (below) unconditionally constructs BOTH accounts' bundles in one call
    # (Task 4's dual-active design: one process, both pipelines always
    # live), so reaching it AT ALL still needs real Alpaca credentials for
    # paper's own AlpacaBroker construction, regardless of which account
    # `--account` ultimately selects. A shadow-only CLI invocation that
    # never needs `build_components` at all -- "strategies", "rearm", plain
    # "reviews list"/"reject" -- has no such requirement (see the `else`
    # branch below, which builds a bare per-account store/queue with no
    # broker anywhere in the picture).
    needs_broker = args.command in {"status", "check", "run", "chat", "serve"} or (
        args.command == "reviews" and getattr(args, "reviews_command", None) == "approve"
        and _approve_needs_broker(settings, args.review_id, account)) or (
        args.command == "memory" and getattr(args, "memory_command", None) == "consolidate")

    broker: Broker | None = None
    if broker_factory is not None:
        broker = broker_factory(settings)
    elif needs_broker:
        if not (settings.alpaca_api_key and settings.alpaca_secret_key):
            print("Missing credentials: set ALPACA_API_KEY / ALPACA_SECRET_KEY "
                  "in .env (see .env.example)", file=sys.stderr)
            return 2
        broker = _default_broker(settings)

    if args.command == "serve":
        return cmd_serve(settings, args.host, args.port)

    if broker is not None:
        from allpath_trade.app import build_components

        components = build_components(settings, broker=broker)
        bundle = components.accounts[account]
        store = bundle.strategies
        queue = bundle.queue
        sentinel = bundle.sentinel
    else:
        from allpath_trade.store.reviews import ReviewQueue
        from allpath_trade.strategy.store import StrategyStore

        conn = connect(settings.db_path)
        # shadow-dual-active T2: this branch never calls build_components,
        # so it never runs migrate_files() -- it only reads/writes the
        # already-canonical strategies/{account}/ location (a prior
        # build_components call, or a fresh install with nothing to
        # migrate, is what put it there; see migrate_files.py's docstring).
        # Use classmethod for account validation (T4 shadow-bundle gate).
        # shadow-dual-active T5: this branch (broker is None) is reached
        # for EITHER account -- "strategies"/"rearm"/plain "reviews
        # list"/"reject" never need a broker/executor/sentinel regardless of
        # which account they're scoped to, so `account` here can genuinely
        # be "shadow", not just the "paper" this comment used to assume.
        store = StrategyStore.for_account(settings.strategies_dir, conn,
                                          account=account)
        queue = ReviewQueue(conn, executor=None, account=account)
        # Wired unconditionally, same as build_components does for the
        # broker branch: approving a strategy_revision row is plain file
        # I/O, not broker-dependent, so it must work in this no-broker path
        # too (that's the whole point of Finding 6's fix above).
        queue.set_revision_applier(apply_revision_factory(store))
        sentinel = None

    if args.command == "status":
        # "status" is always in `needs_broker` (see above), so `broker`
        # is never None here and `bundle` -- the right broker instance for
        # THIS account (Alpaca for paper, ShadowLedger for shadow) -- is
        # always defined.
        return cmd_status(settings, bundle.broker, account=account)

    if args.command == "check":
        return cmd_check(sentinel)
    if args.command == "run":
        from allpath_trade.scheduler import run_daemon, run_daily_jobs

        # Same daily sequence as `serve`'s build_jobs -- digest, then each
        # account's gated reflection + gated consolidation -- via the
        # shared helper in scheduler.py so the two entry points can't
        # drift out of sync again (docs/TODO.md's Phase 5 leftover this
        # closes: the headless `run` daemon used to skip the digest
        # entirely and never gated consolidation on `daily_consolidation`).
        # shadow-dual-active T4 (cli run parity): `run_daemon` now iterates
        # `components.accounts` -- both paper and shadow get their own
        # sentinel pass and nightly chain from this one daemon, matching
        # `serve`'s build_jobs exactly. `lambda: components.accounts`
        # mirrors build_jobs's own `holder.get()` indirection even though
        # this particular `components` object never gets rebuilt within
        # one `run` process.
        run_daemon(lambda: components.accounts, settings.sentinel_interval_minutes,
                   daily_job=lambda: run_daily_jobs(components, verbose=True),
                   app_state=components.app_state)
        return 0
    if args.command == "strategies":
        return cmd_strategies(settings, store)
    if args.command == "rearm":
        return cmd_rearm(store, args.strategy_id, args.rule_id)
    if args.command == "reviews":
        return cmd_reviews(queue, args, store)
    if args.command == "chat":
        from allpath_trade.llm.factory import LLMConfigError, build_llm

        factory = llm_factory or build_llm
        try:
            # TODO: neither call below passes usage_store=components.
            # llm_usage the way cmd_chat's own internal build_llm call does
            # (see that function's `if memory_llm is None` branch) -- this
            # `factory` indirection's signature is `Callable[[Settings,
            # str], LLMClient]` (2 positional args, no usage_store), shared
            # with every test's injected `llm_factory`, so widening it here
            # would mean updating every one of those fakes too. Left
            # unwired for now: `cli chat` calls made through THIS factory
            # simply go unrecorded in the Usage panel/daily-digest cost
            # line (llm/factory.py's `_RecordingClient` is the only thing
            # that ever writes to `LLMUsage`, and nothing here constructs
            # one) -- not a correctness bug, just an undercount for this
            # one entry point.
            llm = factory(settings, "chat")
            # Same factory (real or injected) as the chat-tier client, so a
            # test that scripts one scripts the other: the Compactor's LLM
            # calls stay fully deterministic instead of quietly reaching a
            # real provider the moment compaction triggers.
            memory_llm = factory(settings, "memory")
        except LLMConfigError as exc:
            print(f"LLM not configured: {exc}", file=sys.stderr)
            return 2
        # shadow-dual-active T5: `cmd_chat` reads a flat object's
        # `.conn`/`.data`/`.broker`/`.journal`/`.strategies`/`.queue`/
        # `.executor`/`.memory`/`.consolidator` (account-scoped) AND
        # `.settings`/`.llm_usage` (shared) off the SAME argument -- for
        # paper, `components` already carries all of those (the legacy
        # alias surface, see app.py's `Components` docstring), unchanged
        # from before this task. For shadow, `components.accounts["shadow"]`
        # (`AccountComponents`) has the first group but not `.settings`/
        # `.llm_usage` by design (Task 4: those stay shared, off the parent
        # `Components` only) -- `_cli_chat_bundle` below bridges the two
        # into one object `cmd_chat` can read from either account without
        # `cmd_chat` itself needing to know about accounts at all.
        return cmd_chat(_cli_chat_bundle(components, account), llm,
                        new=args.new, memory_llm=memory_llm)
    if args.command == "memory":
        if args.memory_command == "show":
            from allpath_trade.memory.store import MemoryStore

            memory = MemoryStore(settings.memory_dir, connect(settings.db_path),
                                 account=account)
            return cmd_memory_show(memory, args.layer, args.key)
        if args.memory_command == "consolidate":
            consolidator = components.accounts[account].consolidator
            if consolidator is None:
                print("LLM not configured: set OPENROUTER_API_KEY "
                      "(or provider key) in .env", file=sys.stderr)
                return 2
            print(consolidator.run_daily())
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
