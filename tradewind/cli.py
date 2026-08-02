from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from tradewind.broker.base import Broker
from tradewind.config import Settings, SettingsStore
from tradewind.llm.base import LLMClient
from tradewind.store.db import connect
from tradewind.store.journal import TradeJournal


def _default_broker(settings: Settings) -> Broker:
    from tradewind.broker.alpaca import AlpacaBroker

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
    from tradewind.strategy.loader import StrategyValidationError

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
    from tradewind.store.reviews import ReviewError

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
        text = memory.read(layer, key)
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
          | \        t r a d e w i n d
          |  \       your mid/long-term investing copilot
       ___|___\__
       \_________/
    ~ ~ ~ ~ ~ ~ ~ ~
"""


def cmd_chat(components, llm, *, new: bool, input_fn=None) -> int:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel

    from tradewind.agent.action_tools import register_action_tools
    from tradewind.agent.context import build_system_prompt, load_identity
    from tradewind.agent.loop import AgentSession
    from tradewind.agent.memory_tools import register_memory_tools
    from tradewind.agent.readonly_tools import register_readonly_tools
    from tradewind.agent.tools import ToolRegistry
    from tradewind.memory.search import SessionSearch
    from tradewind.store.conversations import ConversationStore

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

    # Colored input prompt only on a real terminal (tests/pipes get plain text).
    prompt = "\x1b[1;36myou ▸ \x1b[0m" if sys.stdout.isatty() else "you ▸ "
    while True:
        try:
            user = input_fn(prompt)
        except EOFError:
            console.print()
            return 0
        if user.strip() in ("/exit", "/quit"):
            if components.consolidator is not None:
                new_msgs = session.history[initial_len:]
                if new_msgs:
                    try:
                        note = components.consolidator.run_post_chat(new_msgs)
                        console.print(f"[dim]memory: {note}[/dim]")
                    except Exception:  # noqa: BLE001, S110 — exit must never fail
                        pass
            console.print("[dim]bye — the sentinel keeps watching your rules.[/dim]")
            return 0
        if not user.strip():
            continue
        reply = session.run_turn(user)
        console.print("\n[bold magenta]◆ tradewind[/bold magenta]")
        console.print(Markdown(reply or "(no reply)"))
        console.print()


def main(argv: list[str] | None = None,
         broker_factory: Callable[[Settings], Broker] | None = None,
         llm_factory: Callable[[Settings, str], LLMClient] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tradewind")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show account, positions, recent trades")
    sub.add_parser("check", help="run one sentinel pass now")
    sub.add_parser("run", help="run the sentinel daemon")
    sub.add_parser("strategies", help="list strategies and rule states")
    p_rearm = sub.add_parser("rearm", help="re-arm a triggered rule")
    p_rearm.add_argument("strategy_id")
    p_rearm.add_argument("rule_id")
    p_reviews = sub.add_parser("reviews", help="pending trigger reviews")
    rsub = p_reviews.add_subparsers(dest="reviews_command", required=True)
    rsub.add_parser("list")
    p_app = rsub.add_parser("approve")
    p_app.add_argument("review_id", type=int)
    p_rej = rsub.add_parser("reject")
    p_rej.add_argument("review_id", type=int)
    p_rej.add_argument("--note", default="")
    p_chat = sub.add_parser("chat", help="talk to the agent")
    p_chat.add_argument("--new", action="store_true",
                        help="start a new conversation instead of resuming")
    p_mem = sub.add_parser("memory", help="curated agent memory")
    msub = p_mem.add_subparsers(dest="memory_command", required=True)
    m_show = msub.add_parser("show")
    m_show.add_argument("--layer", default=None,
                        help="memory layer (profile, strategy, stock, lesson)")
    m_show.add_argument("--key", default=None,
                        help="memory key (strategy/stock name)")
    msub.add_parser("consolidate")
    args = parser.parse_args(argv)

    settings = SettingsStore().load()

    # Only commands that actually reach the broker require credentials;
    # read-only commands (strategies, rearm, reviews list/reject) work without.
    needs_broker = args.command in {"status", "check", "run", "chat"} or (
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

    if broker is not None:
        from tradewind.app import build_components

        components = build_components(settings, broker=broker)
        store = components.strategies
        queue = components.queue
        sentinel = components.sentinel
    else:
        from tradewind.store.reviews import ReviewQueue
        from tradewind.strategy.store import StrategyStore

        conn = connect(settings.db_path)
        settings.strategies_dir.mkdir(parents=True, exist_ok=True)
        store = StrategyStore(settings.strategies_dir, conn)
        queue = ReviewQueue(conn, executor=None)
        sentinel = None

    if args.command == "check":
        return cmd_check(sentinel)
    if args.command == "run":
        from tradewind.scheduler import run_daemon

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
        from tradewind.llm.factory import LLMConfigError, build_llm

        try:
            llm = (llm_factory or build_llm)(settings, "chat")
        except LLMConfigError as exc:
            print(f"LLM not configured: {exc}", file=sys.stderr)
            return 2
        return cmd_chat(components, llm, new=args.new)
    if args.command == "memory":
        if args.memory_command == "show":
            from tradewind.memory.store import MemoryStore

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
