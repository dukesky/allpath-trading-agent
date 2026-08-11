from __future__ import annotations

import difflib
from collections.abc import Callable
from pathlib import Path

import yaml

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from allpath_trade.strategy.loader import (
    StrategyValidationError,
    atomic_write_text,
    is_valid_strategy_id,
    parse_strategy_text,
)
from allpath_trade.strategy.store import StrategyStore

# Tool-arg hygiene: a reflection session's rationale is model-generated free
# text with no other length limit upstream -- caps how much of it lands in
# the DB row and (later) back in a future agent's context.
_MAX_RATIONALE_CHARS = 2000


def register_reflection_tools(registry: ToolRegistry, *, strategies: StrategyStore,
                              queue: ReviewQueue) -> None:
    """Registers the one tool a reflection session gets for changing a
    strategy: `propose_strategy_revision`. There is deliberately no
    apply/confirm tool here -- every revision goes through the same human
    Pending-page approval as everything else (spec §④: "无下单、无确认类
    工具")."""

    def propose_strategy_revision(strategy_id: str, new_yaml: str, rationale: str) -> str:
        rationale = rationale.strip()[:_MAX_RATIONALE_CHARS]
        if not rationale:
            return "error: rationale is required"
        # Same id-validation gate the web uses (web/routes/strategies.py) --
        # rejects path traversal / absolute paths before they ever reach the
        # filesystem.
        if not is_valid_strategy_id(strategy_id):
            return f"error: invalid strategy id {strategy_id!r}"
        path = strategies.directory / f"{strategy_id}.yaml"
        if not path.exists():
            return f"error: strategy '{strategy_id}' not found"

        # parse_strategy_text (below) always forces raw["id"] = strategy_id
        # before validating -- which would silently swallow an attempted id
        # change rather than rejecting it. So the *declared* id has to be
        # checked against the raw YAML here, before that override happens.
        try:
            raw = yaml.safe_load(new_yaml)
        except yaml.YAMLError as exc:
            return (f"error: invalid strategy revision for '{strategy_id}': "
                    f"YAML parse error: {exc}")
        if isinstance(raw, dict) and "id" in raw and raw["id"] != strategy_id:
            return (f"error: invalid strategy revision for '{strategy_id}': "
                    f"cannot change strategy id (proposed id {raw['id']!r})")

        try:
            doc = parse_strategy_text(strategy_id, new_yaml)
        except StrategyValidationError as exc:
            return f"error: invalid strategy revision for '{strategy_id}': {exc}"

        old_yaml = path.read_text()
        diff = "\n".join(difflib.unified_diff(
            old_yaml.splitlines(), new_yaml.splitlines(),
            fromfile=f"{strategy_id} (current)", tofile=f"{strategy_id} (proposed)",
            lineterm=""))
        rid = queue.add_strategy_revision(
            strategy_id=strategy_id, ticker=doc.position.ticker,
            old_yaml=old_yaml, new_yaml=new_yaml, diff=diff, rationale=rationale)
        return (f"Revision queued for user approval (#{rid}). It will not "
                "take effect unless approved.")

    t = "string"
    registry.register(
        "propose_strategy_revision",
        "Propose a revision to an existing strategy's YAML, based on "
        "reflection over recent performance. This queues the change for "
        "the user's approval on the Pending page -- it is never applied "
        "automatically.",
        {"type": "object", "properties": {
            "strategy_id": {"type": t}, "new_yaml": {"type": t},
            "rationale": {"type": t}},
         "required": ["strategy_id", "new_yaml", "rationale"]},
        propose_strategy_revision)


def apply_revision_factory(strategies_dir: Path,
                           store: StrategyStore) -> Callable[[str, str], None]:
    """Builds the applier `ReviewQueue.set_revision_applier` calls when a
    strategy_revision row is approved. Lives alongside
    `propose_strategy_revision` (rather than in strategy/) because it is
    that tool's write-side counterpart and shares its re-validation call --
    keeping propose+apply together beats splitting one paired flow across
    packages."""

    def apply(strategy_id: str, new_yaml: str) -> None:
        # Re-validate against the PROPOSED yaml (not the file on disk) --
        # the file may have changed since the proposal was queued (spec §④:
        # a same-strategy same-day second proposal, approved after an
        # earlier one already rewrote the file). This is the only check
        # that runs before any write, and it MUST raise
        # RevisionValidationError -- and only that -- on failure:
        # ReviewQueue._approve_revision rolls the claim back to "pending"
        # solely for this exception, on the invariant that it only ever
        # fires pre-write (see the loud comment there).
        try:
            doc = parse_strategy_text(strategy_id, new_yaml)
        except StrategyValidationError as exc:
            raise RevisionValidationError(str(exc)) from exc
        path = strategies_dir / f"{strategy_id}.yaml"
        atomic_write_text(path, new_yaml)
        # notify_email preservation: re-validation intentionally checks only
        # the PROPOSED yaml, not a diff against the current file -- the
        # proposal already carried the full file verbatim, so a proposal
        # built from a stale base that dropped a later notify_email change
        # is exactly what re-validation here cannot catch. The user reviews
        # a diff regenerated against the CURRENT file at approval time
        # (Task 6's route), so what they approve is what they actually saw.
        store.snapshot_version(doc, reason="reflection revision approved via web")

    return apply
