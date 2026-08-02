from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from allpath_trade.broker.base import Broker
from allpath_trade.config import Settings, SettingsStore
from allpath_trade.llm.base import LLMClient
from allpath_trade.store.db import connect
from allpath_trade.store.journal import TradeJournal


def _default_broker(settings: Settings) -> Broker:
    from allpath_trade.broker.alpaca import AlpacaBroker

    return AlpacaBroker(settings.alpaca_api_key, settings.alpaca_secret_key,
                        paper=settings.alpaca_paper)


def cmd_status(settings: Settings, broker: Broker) -> int:
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

    journal = TradeJournal(connect(settings.db_path))
    rows = journal.recent(limit=5)
    if rows:
        print("\nrecent trades:")
        for r in rows:
            print(f"  #{r['id']} {r['ts'][:19]} {r['side']} {r['ticker']} "
                  f"[{r['status']}] {r['reason']}")
    return 0


def cmd_serve(settings: Settings, host: str | None, port: int | None) -> int:
    import uvicorn

    from allpath_trade.web.app import create_app

    host = host or settings.web_host
    port = port or settings.web_port
    app = create_app(settings, start_scheduler=True)
    shown = "localhost" if host in {"127.0.0.1", "localhost"} else host
    print(f"[allpath-trade] http://{shown}:{port}")
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
        print("no strategies found in", settings.strategies_dir)
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


def cmd_reviews(q, args) -> int:
    from allpath_trade.store.reviews import ReviewError

    try:
        if args.reviews_command == "list":
            rows = q.list()
            if not rows:
                print("no pending reviews")
            for r in rows:
                print(f"#{r['id']} {r['ts'][:19]} {r['strategy_id']}/{r['rule_id']} "
                      f"[{r['status']}] {r['condition']} -> {r['action']}")
        elif args.reviews_command == "approve":
            result = q.approve(args.review_id)
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


def cmd_chat(components, llm, *, new: bool, input_fn=None) -> int:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    from allpath_trade.agent.action_tools import register_action_tools
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
    session = AgentSession(llm, registry, system, store=store,
                           conversation_id=cid, on_tool=on_tool)
    initial_len = len(session.history)

    mode = "paper" if components.broker.is_paper else "LIVE"
    console.print(f"[bold cyan]{CHAT_BANNER}[/bold cyan]")
    console.print(f"  [dim]model[/dim]  [bold]{llm.model}[/bold] "
                  f"[dim]via {components.settings.llm_provider} · {mode} account[/dim]")
    console.print(f"  [dim]chat[/dim]   conversation #{cid} · /exit to quit · "
                  "orders & strategy changes always ask first\n")

    def _finish() -> None:
        if components.consolidator is not None:
            new_msgs = session.history[initial_len:]
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


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None,
         llm_factory: Callable[[Settings, str], LLMClient] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="allpath-trade", description=CLI_DESCRIPTION, epilog=CLI_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True,
                                metavar="<command>")
    sub.add_parser(
        "status", help="show account, positions, recent trades",
        description="Show brokerage account (equity/cash/buying power), open "
                    "positions, and the last 5 journaled trades. "
                    "Requires ALPACA_API_KEY / ALPACA_SECRET_KEY in .env.")
    sub.add_parser(
        "check", help="run one sentinel pass now",
        description="Evaluate every armed rule of every active strategy once, "
                    "right now — useful for testing a strategy you just wrote. "
                    "Hard rules on auto-strategies may EXECUTE (paper) trades.")
    sub.add_parser(
        "run", help="run the monitoring daemon",
        description="Long-running daemon: sentinel pass every "
                    "SENTINEL_INTERVAL_MINUTES (default 60) during US market "
                    "hours, plus the daily memory consolidation after close.")
    sub.add_parser(
        "strategies", help="list strategies and rule states",
        description="List every strategy YAML in strategies/ with its status, "
                    "authorization level, and each rule's armed/triggered state. "
                    "Works without any keys.")
    p_rearm = sub.add_parser(
        "rearm", help="re-arm a triggered rule",
        description="Rules fire once, then stay 'triggered' until re-armed. "
                    "Example: allpath-trade rearm aapl-long stop-loss")
    p_rearm.add_argument("strategy_id", help="strategy file name without .yaml")
    p_rearm.add_argument("rule_id", help="rule id inside the strategy")
    p_reviews = sub.add_parser(
        "reviews", help="approve/reject triggers awaiting review",
        description="Soft-rule triggers (and everything on confirm-level "
                    "strategies) queue here with the agent's analysis attached. "
                    "Approving executes through the risk gate.")
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
    p_mem = sub.add_parser(
        "memory", help="inspect or consolidate the agent's memory",
        description="The agent's curated memory lives in memory/ as plain "
                    "markdown you can read and edit. Layers: profile, strategy, "
                    "stock, lesson.")
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
                    "process. Requires Alpaca keys in .env.")
    p_serve.add_argument("--host", default=None, help="bind address")
    p_serve.add_argument("--port", type=int, default=None, help="port")

    if argv is not None and len(argv) == 0:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)

    settings = SettingsStore().load()

    # Only commands that actually reach the broker require credentials;
    # read-only commands (strategies, rearm, reviews list/reject) work without.
    needs_broker = args.command in {"status", "check", "run", "chat", "serve"} or (
        args.command == "reviews" and getattr(args, "reviews_command", None) == "approve") or (
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

    if args.command == "status":
        return cmd_status(settings, broker)

    if args.command == "serve":
        return cmd_serve(settings, args.host, args.port)

    if broker is not None:
        from allpath_trade.app import build_components

        components = build_components(settings, broker=broker)
        store = components.strategies
        queue = components.queue
        sentinel = components.sentinel
    else:
        from allpath_trade.store.reviews import ReviewQueue
        from allpath_trade.strategy.store import StrategyStore

        conn = connect(settings.db_path)
        settings.strategies_dir.mkdir(parents=True, exist_ok=True)
        store = StrategyStore(settings.strategies_dir, conn)
        queue = ReviewQueue(conn, executor=None)
        sentinel = None

    if args.command == "check":
        return cmd_check(sentinel)
    if args.command == "run":
        from allpath_trade.scheduler import run_daemon

        daily = None
        if components.consolidator is not None:
            daily = lambda: print("[memory] " + components.consolidator.run_daily())
        run_daemon(lambda: sentinel, settings.sentinel_interval_minutes,
                   daily_job=daily)
        return 0
    if args.command == "strategies":
        return cmd_strategies(settings, store)
    if args.command == "rearm":
        return cmd_rearm(store, args.strategy_id, args.rule_id)
    if args.command == "reviews":
        return cmd_reviews(queue, args)
    if args.command == "chat":
        from allpath_trade.llm.factory import LLMConfigError, build_llm

        try:
            llm = (llm_factory or build_llm)(settings, "chat")
        except LLMConfigError as exc:
            print(f"LLM not configured: {exc}", file=sys.stderr)
            return 2
        return cmd_chat(components, llm, new=args.new)
    if args.command == "memory":
        if args.memory_command == "show":
            from allpath_trade.memory.store import MemoryStore

            memory = MemoryStore(settings.memory_dir, connect(settings.db_path))
            return cmd_memory_show(memory, args.layer, args.key)
        if args.memory_command == "consolidate":
            if components.consolidator is None:
                print("LLM not configured: set OPENROUTER_API_KEY "
                      "(or provider key) in .env", file=sys.stderr)
                return 2
            print(components.consolidator.run_daily())
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
