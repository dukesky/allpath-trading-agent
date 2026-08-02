from __future__ import annotations

import difflib
from collections.abc import Callable
from decimal import Decimal, InvalidOperation

import yaml
from pydantic import ValidationError

from tradewind.agent.tools import ToolRegistry
from tradewind.broker.base import OrderIntent, OrderSide
from tradewind.execution import ExecutionError, Executor
from tradewind.strategy.loader import StrategyValidationError, parse_strategy_text
from tradewind.strategy.store import StrategyStore


def register_action_tools(registry: ToolRegistry, *, strategies: StrategyStore,
                          executor: Executor,
                          confirm: Callable[[str], bool]) -> None:

    def draft_strategy(strategy_id: str, yaml_text: str, reason: str) -> str:
        try:
            doc = parse_strategy_text(strategy_id, yaml_text)
        except StrategyValidationError as exc:
            return f"error: {'; '.join(exc.errors)}"
        path = strategies.directory / f"{strategy_id}.yaml"
        old_text = path.read_text() if path.exists() else ""
        if old_text:
            try:
                current = parse_strategy_text(strategy_id, old_text)
                if doc.version <= current.version:
                    doc = doc.model_copy(update={"version": current.version + 1})
            except StrategyValidationError:
                pass  # unreadable current file: keep drafted version
        new_text = yaml.safe_dump(doc.model_dump(mode="json"), sort_keys=False,
                                  allow_unicode=True)
        diff = "".join(difflib.unified_diff(
            old_text.splitlines(keepends=True), new_text.splitlines(keepends=True),
            fromfile=f"{strategy_id}.yaml (current)",
            tofile=f"{strategy_id}.yaml (proposed)"))
        if not confirm(f"Save strategy '{strategy_id}' v{doc.version}?"
                       f" Reason: {reason}\n{diff or new_text}"):
            return "user declined"
        path.write_text(new_text)
        strategies.snapshot_version(doc, reason)
        return f"saved {strategy_id} v{doc.version}"

    def propose_order(ticker: str, side: str, reason: str,
                      qty: str | None = None, notional: str | None = None) -> str:
        try:
            intent = OrderIntent(
                ticker=ticker, side=OrderSide(side.lower()),
                qty=Decimal(str(qty)) if qty is not None else None,
                notional=Decimal(str(notional)) if notional is not None else None,
                reason=reason)
        except (ValidationError, ValueError, InvalidOperation) as exc:
            return f"error: invalid order: {exc}"
        size = f"qty {intent.qty}" if intent.qty else f"${intent.notional}"
        if not confirm(f"Submit order: {intent.side.value} {size} "
                       f"{intent.ticker}? Reason: {reason}"):
            return "user declined"
        try:
            result = executor.execute(intent)
        except ExecutionError as exc:
            return f"execution error: {exc}"
        if result.submitted:
            return f"submitted order {result.order.id if result.order else ''}".strip()
        return "rejected by risk gate: " + "; ".join(result.decision.reasons)

    t = "string"
    registry.register(
        "draft_strategy",
        "Draft or revise a strategy YAML. The user must confirm before it is "
        "saved; a version snapshot is recorded.",
        {"type": "object", "properties": {
            "strategy_id": {"type": t}, "yaml_text": {"type": t},
            "reason": {"type": t}},
         "required": ["strategy_id", "yaml_text", "reason"]},
        draft_strategy)
    registry.register(
        "propose_order",
        "Propose a market order (buy/sell). The user must confirm; the order "
        "then passes the deterministic risk gate.",
        {"type": "object", "properties": {
            "ticker": {"type": t}, "side": {"type": t, "enum": ["buy", "sell"]},
            "qty": {"type": t}, "notional": {"type": t}, "reason": {"type": t}},
         "required": ["ticker", "side", "reason"]},
        propose_order)
