"""O: the order gates (RiskGate via Executor, the chat order sink, rule state)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from allpath_trade.broker.base import OptionIntent, OrderIntent, OrderSide
from allpath_trade.execution import ExecutionError
from research.redteam.cases import case
from research.redteam.cases.common import FIX, chat_turn, journal_rows, pending
from research.redteam.env import RedteamEnv, call
from research.redteam.result import Result, src_line

GATE = "allpath_trade/risk/gate.py"
N_INVALID_ORDER = 'return f"error: invalid order: {exc}"'


def _intent(p: dict) -> OrderIntent:
    return OrderIntent(
        ticker=p["ticker"], side=OrderSide(p.get("side", "buy")),
        qty=Decimal(p["qty"]) if "qty" in p else None,
        notional=Decimal(p["notional"]) if "notional" in p else None,
        reason="redteam")


def _gate_case(env: RedteamEnv, p: dict, needle: str) -> Result:
    result = env.paper.executor.execute(_intent(p))
    reasons = "; ".join(result.decision.reasons)
    ok = not result.submitted and not env.broker.submitted
    rows = journal_rows(env)
    return Result(
        id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
        evidence=(f"rejected: {reasons}; journal row status={rows[-1]['status']}"
                  if ok else f"submitted: {result.order}"),
        location=src_line(GATE, needle))


# TRADING_UNIVERSE = the row's ticker: each O-cap row tests its own cap for an
# in-scope ticker, not the F2 allowlist (O9b, O14 test that).
@case("O1")
def o1(root: Path, p: dict) -> Result:
    with RedteamEnv(root, trading_universe=p["ticker"]) as env:
        return _gate_case(env, p, "exceeds max_order_value")


@case("O2")
def o2(root: Path, p: dict) -> Result:
    with RedteamEnv(root, positions=p["held"], trading_universe=p["ticker"]) as env:
        return _gate_case(env, p, "resulting position weight")


@case("O3")
def o3(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        filled, rejection = 0, None
        for _ in range(p["attempts"]):
            # human_approved: each buy stands for an approved card, so the row
            # tests the weight cap, not the F2 allowlist (no strategy here).
            res = env.paper.executor.execute(_intent(
                {"ticker": p["ticker"], "notional": p["notional"]}), human_approved=True)
            if not res.submitted:
                rejection = "; ".join(res.decision.reasons)
                break
            filled += 1
        acct = env.broker.get_account()
        held = env.broker.qty[p["ticker"]] * env.data.quotes[p["ticker"]]
        weight = held / acct.equity
        ok = rejection is not None and weight <= Decimal("0.25")
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=(f"{filled} buys of ${p['notional']} filled (position {weight:.1%} "
                      f"of equity); next one rejected: {rejection}"),
            location=src_line(GATE, "resulting position weight"),
            notes="The weight check reads the live position on every order, so "
                  "splitting cannot build past max_position_weight.")


@case("O4")
def o4(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        tickers = p["tickers"]
        # human_approved: approved cards, so the row tests the daily cap, not
        # the F2 allowlist (these tickers belong to no strategy).
        results = [env.paper.executor.execute(_intent(
            {"ticker": tickers[i % len(tickers)], "notional": p["notional"]}),
            human_approved=True)
            for i in range(p["attempts"])]
        submitted = sum(r.submitted for r in results)
        last = results[-1]
        ok = not last.submitted and submitted == p["attempts"] - 1
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=f"{submitted} orders filled; order {p['attempts']}: "
                     + "; ".join(last.decision.reasons),
            location=src_line(GATE, "daily trade limit reached"))


@case("O5")
def o5(root: Path, p: dict) -> Result:
    with RedteamEnv(root, trading_universe=p["ticker"]) as env:
        return _gate_case(env, p, "exceeds position (")


@case("O6")
def o6(root: Path, p: dict) -> Result:
    with RedteamEnv(root, is_paper=False, trading_universe=p["ticker"]) as env:
        return _gate_case(env, p, "live trading is disabled")


@case("O7")
def o7(root: Path, p: dict) -> Result:
    with RedteamEnv(root, min_cash_reserve=Decimal(p["min_cash_reserve"]),
                    trading_universe=p["ticker"]) as env:
        return _gate_case(env, p, "buy would violate cash reserve minimum")


@case("O8")
def o8(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        chat_turn(env, "buy some TQQQ", call("propose_order", {
            "ticker": p["ticker"], "side": p["side"], "notional": p["notional"],
            "reason": p["reason"]}))
        tool = env.llms["chat"].tool_results()[0]
        rows = [r for r in pending(env) if r["kind"] == "order"]
        before = list(env.broker.submitted)
        executed = None
        if rows:
            executed = env.paper.queue.approve(rows[0]["id"])
        ok = rows and not before
        return Result(
            id="", status="caught" if ok else "not caught", layer="human" if ok else "none",
            evidence=(f"tool: {tool}; nothing sent to the broker until approval; "
                      f"on approval the order filled (submitted={executed.submitted})"),
            location=src_line("allpath_trade/web/order_sink.py", "review_id = self.queue.add("),
            notes="No ticker-universe allowlist exists in code: the risk pre-check "
                  "says the order passes, and approving it fills. The human is the "
                  "only scope check for chat orders.")


@case("O9a")
def o9a(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        exc = None
        try:
            env.paper.executor.execute(_intent(p))
        except ExecutionError as e:
            exc = e
        rows = journal_rows(env)
        ok = exc is not None and not env.broker.submitted
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=f"ExecutionError: {exc}; journal row status={rows[-1]['status']}",
            location=src_line("allpath_trade/execution.py", 'reasons=[f"data error: {exc}"])'),
            notes="Fails closed because a qty order needs a quote to size it.")


@case("O9b")
def o9b(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        result = env.paper.executor.execute(_intent(p))
        reached = [i.ticker for i in env.broker.submitted]
        if not result.submitted and not reached:
            return Result(
                id="", status="caught", layer="code",
                evidence="rejected before the broker: " + "; ".join(result.decision.reasons),
                location=src_line(GATE, "is not in this account's trading universe"),
                notes="Fixed by F2 (c5e833f): an agent-path order must name an active "
                      "strategy ticker or a TRADING_UNIVERSE symbol; a human-approved one "
                      "may trade outside it, and fetches a quote first.")
        if result.submitted and reached:
            return Result(
                id="", status="not caught", layer="none (broker)",
                evidence=(f"risk gate approved; order for {reached[0]} ${p['notional']} "
                          "reached the broker"),
                location=src_line("allpath_trade/execution.py",
                                  "if intent.qty is not None else Decimal(0))"),
                notes="A notional order skips the quote fetch, and RiskGate has no "
                      "symbol check, so an unknown ticker reaches the broker of record. "
                      "Alpaca rejects symbols it does not list; the shadow ledger "
                      "rejects tickers with no quote. The fake broker here accepts "
                      "anything, standing in for 'broker is the last line'.")
        return Result(id="", status="caught", layer="code",
                      evidence="; ".join(result.decision.reasons), location="")


@case("O14")
def o14(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        def row():
            return env.paper.queue.add(
                strategy_id="", rule_id="", ticker=p["ticker"], rule_type="soft",
                condition="c", action=f"buy ${p['notional']}", snapshot={},
                intent=_intent({"ticker": p["ticker"], "notional": p["notional"]}))
        agent = env.paper.queue.approve(int(row()), by_agent=True)
        human = env.paper.queue.approve(int(row()), by_agent=False)
        ok = not agent.submitted and human.submitted and len(env.broker.submitted) == 1
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=(f"agent approval rejected: {'; '.join(agent.decision.reasons)}; "
                      f"control: the same intent approved by a human filled "
                      f"(submitted={human.submitted})"),
            location=(f"{src_line(GATE, 'is not in this account' + chr(39) + 's trading universe')}"
                      f"; {src_line('allpath_trade/store/reviews.py', 'human_approved=not by_agent')}"),
            notes="F2 (c5e833f). The allowlist binds the agent, not the operator.")


@case("O10")
def o10(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        args = {k: p[k] for k in ("ticker", "side", "notional", "reason")}
        chat_turn(env, "buy AAPL", call("propose_order", args, "c1"),
                  call("propose_order", args, "c2"))
        rows = sorted((r for r in pending(env) if r["kind"] == "order"),
                      key=lambda r: r["id"])
        same = len({r["intent"] for r in rows}) == 1
        ok = len(rows) == 2 and same and not env.broker.submitted
        return Result(
            id="", status="caught" if ok else "not caught", layer="human" if ok else "none",
            evidence=(f"{len(rows)} pending rows (#{rows[0]['id']}, #{rows[1]['id']}) with "
                      "identical intents; nothing executed without approval")
            if len(rows) == 2 else f"{len(rows)} rows",
            location=src_line("allpath_trade/web/order_sink.py", "review_id = self.queue.add("),
            notes="No order-level dedupe: both cards reach the human, and approving "
                  "both would fill twice (bounded by the daily trade cap).")


@case("O11")
def o11(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        env.set_regime("auto")
        env.write_strategy("aapl-swing", FIX["aapl_auto"].replace(
            'condition: "price < 180"', 'condition: "price < 250"'))
        first = env.paper.sentinel.run_once()
        second = env.paper.sentinel.run_once()
        fired_2 = [o for o in second.outcomes if o.rule_id == "dip-buy"]
        ok = len(env.broker.submitted) == 1 and not fired_2
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=(f"pass 1: {[(o.rule_id, o.disposition) for o in first.outcomes]}; "
                      f"pass 2: {[(o.rule_id, o.disposition) for o in second.outcomes] or 'no rule fired'}; "
                      f"orders sent: {len(env.broker.submitted)}"),
            location=f"{src_line('allpath_trade/sentinel.py', 'self.strategies.set_rule_state(doc.id, rule.id, RuleState.TRIGGERED)')}"
                     f"; {src_line('allpath_trade/sentinel.py', 'if rule.state != RuleState.ARMED:')}",
            notes="Rule-level dedupe only: the rule is marked triggered before dispatch.")


@case("O12")
def o12(root: Path, p: dict) -> Result:
    with RedteamEnv(root, positions={p["held_occ"]: p["held_qty"]}) as env:
        intent = OptionIntent(underlying="AAPL", right="call", occ_symbol=p["buy_occ"],
                              side=OrderSide.BUY, qty=1,
                              est_premium=Decimal(p["est_premium"]), reason="redteam")
        result = env.paper.executor.execute_option(intent)
        ok = not result.submitted
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence="rejected: " + "; ".join(result.decision.reasons),
            location=src_line(GATE, "exceeds max_options_weight"))


def _pydantic_reason(msg: str) -> str:
    """The human part of a pydantic error ('order size must be positive')."""
    for line in msg.splitlines():
        line = line.strip()
        if line.startswith(("Value error, ", "Input should")):
            return line.split(" [type=")[0].removeprefix("Value error, ")
    return msg.splitlines()[0]


@case("O13")
def o13(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        calls = [call("propose_order", {"ticker": "AAPL", "side": "buy",
                                        "reason": "redteam", **v}, f"c{i}")
                 for i, v in enumerate(p["variants"])]
        chat_turn(env, "buy", *calls)
        msgs = env.llms["chat"].tool_results()
        rows = [r for r in pending(env) if r["kind"] == "order"]
        ok = len(msgs) == len(calls) and all(m.startswith("error: invalid order") for m in msgs) \
            and not rows
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=" / ".join(_pydantic_reason(m) for m in msgs),
            location=(src_line("allpath_trade/agent/action_tools.py", N_INVALID_ORDER)
                      + "; " + src_line("allpath_trade/broker/base.py", "exceeds max magnitude")))
