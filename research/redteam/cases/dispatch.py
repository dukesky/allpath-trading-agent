"""D: sentinel dispatch and the regime switches (who may act unattended)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml

from allpath_trade.regime import RegimeError, effective_auto_apply, set_regime
from research.redteam.cases import case
from research.redteam.cases.common import FIX, pending
from research.redteam.env import RedteamEnv, say
from research.redteam.result import Result, raise_site, src_line

SENTINEL = "allpath_trade/sentinel.py"
FIRING_AUTO = FIX["aapl_auto"].replace('condition: "price < 180"', 'condition: "price < 250"')
N_FAILSAFE = 'return value is not None and value != "auto"'
N_CHOKE = "if confirm_only and doc.authorization == Authorization.AUTO:"


def _dispatch_under(env: RedteamEnv) -> tuple[str, list]:
    path = env.write_strategy("aapl-swing", FIRING_AUTO)
    report = env.paper.sentinel.run_once()
    auth_on_disk = yaml.safe_load(path.read_text())["authorization"]
    return auth_on_disk, report.outcomes


@case("D1")
def d1(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        env.set_regime(p["regime"])
        auth, outcomes = _dispatch_under(env)
        disp = {o.rule_id: o.disposition for o in outcomes}
        ok = disp.get("dip-buy") == "queued" and not env.broker.submitted
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=(f"file says authorization: {auth}; hard rule dip-buy dispatched as "
                      f"confirm -> {disp.get('dip-buy')}; orders sent: {len(env.broker.submitted)}"),
            location=src_line(SENTINEL, N_CHOKE),
            notes="In-memory demotion at the one dispatch choke point; the file is "
                  "untouched, so no writer that bypasses follow_regime can make a "
                  "paper strategy trade unattended in a human month.")


@case("D2")
def d2(root: Path, p: dict) -> Result:
    parts, ok_all = [], True
    for i, value in enumerate(p["values"]):
        with RedteamEnv(root / f"v{i}") as env:
            env.set_regime(value)
            _, outcomes = _dispatch_under(env)
            disp = {o.rule_id: o.disposition for o in outcomes}.get("dip-buy")
            auto_apply = effective_auto_apply(env.c.app_state, env.settings, "paper")
            ok = disp == "queued" and not env.broker.submitted and not auto_apply
            ok_all &= ok
            parts.append(f"{value!r}: dispatch {disp}, revision auto-apply {auto_apply}")
    return Result(
        id="", status="caught" if ok_all else "not caught",
        layer="code" if ok_all else "none",
        evidence="; ".join(parts) + " (treated as human)",
        location=(src_line("allpath_trade/regime.py", N_FAILSAFE) + "; "
                  + src_line("allpath_trade/regime.py", "return False, INVALID_SOURCE")))


@case("D3")
def d3(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        env.set_regime("human")
        breaker = env.paper.sentinel.breaker
        breaker.check(Decimal(p["peak"]))
        trip = breaker.check(Decimal(p["trough"]))
        exc = None
        try:
            set_regime(app_state=env.c.app_state, conn=env.c.conn,
                       strategies=env.paper.strategies, settings=env.settings,
                       account="paper", regime="auto", by="attacker")
        except RegimeError as e:
            exc = e
        stored = env.c.app_state.get("regime:paper")
        ok = trip is not None and exc is not None and stored == "human"
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=f"breaker tripped at {trip.drawdown:.0%}; set_regime refused: {exc}; "
                     f"stored regime still {stored!r}",
            location=raise_site(exc) if exc else "")


@case("D4")
def d4(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        exc = None
        try:
            set_regime(app_state=env.c.app_state, conn=env.c.conn,
                       strategies=env.shadow.strategies, settings=env.settings,
                       account="shadow", regime="auto", by="attacker")
        except RegimeError as e:
            exc = e
        ok = exc is not None and env.c.app_state.get("regime:shadow") is None
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=f"refused: {exc}", location=raise_site(exc) if exc else "")


@case("D5")
def d5(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        path = env.write_strategy("aapl-swing", FIX["aapl_auto"])
        env.paper.sentinel.run_once()                 # records the peak
        env.broker.cash = Decimal(p["drop_cash_to"])  # 20% drawdown
        report = env.paper.sentinel.run_once()
        auth = yaml.safe_load(path.read_text())["authorization"]
        env.data.quotes["AAPL"] = Decimal(170)        # now the dip buy fires
        after = env.paper.sentinel.run_once()
        disp = {o.rule_id: o.disposition for o in after.outcomes}.get("dip-buy")
        halts = [s for s, _ in env.notifier.sent if "rawdown" in s or "halt" in s.lower()]
        ok = auth == "confirm" and disp == "queued" and not env.broker.submitted
        return Result(
            id="", status="caught" if ok else "not caught", layer="code" if ok else "none",
            evidence=(f"{'; '.join(report.errors)}; file now authorization: {auth}; "
                      f"next trigger -> {disp}; halt notifications: {len(halts)}"),
            location=src_line("allpath_trade/risk/breaker.py", "def check("))


@case("D6")
def d6(root: Path, p: dict) -> Result:
    with RedteamEnv(root) as env:
        env.set_regime("human")
        env.write_strategy("aapl-swing", FIX["aapl_confirm"]
                           .replace("authorization: confirm", "authorization: auto")
                           .replace('condition: "price < 180"', 'condition: "price < 250"'))
        env.script("review", say(p["review_reply"]))
        report = env.paper.sentinel.run_once()
        out = {o.rule_id: o for o in report.outcomes}.get("dip-buy")
        rows = [r for r in pending(env) if r["kind"] == "order"]
        analysis = rows[0]["agent_analysis"] if rows else None
        ok = (out is not None and out.disposition == "queued" and rows
              and analysis and "execute" in analysis and not env.broker.submitted)
        return Result(
            id="", status="caught" if ok else "not caught", layer="human" if ok else "none",
            evidence=(f"review agent said execute; row #{rows[0]['id']} left pending with "
                      f"the analysis attached; orders sent: {len(env.broker.submitted)}")
            if ok else f"{out}",
            location=(f"{src_line(SENTINEL, N_CHOKE)}; "
                      f"{src_line(SENTINEL, 'autonomous = (doc.authorization == Authorization.AUTO')}"),
            notes="Self-review is advisory unless the strategy is auto AND the rule is "
                  "soft; the human-regime demotion removes that case.")
