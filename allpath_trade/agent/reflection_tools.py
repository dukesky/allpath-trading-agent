from __future__ import annotations

import difflib
from collections.abc import Callable
from typing import Optional

import yaml

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.store.reviews import ReviewQueue
from allpath_trade.strategy.apply import apply_revision_factory
from allpath_trade.strategy.loader import (
    StrategyValidationError,
    is_valid_strategy_id,
    parse_strategy_text,
)
from allpath_trade.strategy.model import Authorization
from allpath_trade.strategy.store import StrategyStore

# Re-exported so existing importers (cli.py, app.py, tests) keep working
# unchanged -- the applier itself now lives in strategy/apply.py (it's the
# strategy write path shared by reflection and chat proposals, not
# reflection-specific anymore; see that module's docstring).
__all__ = ["apply_revision_factory", "register_reflection_tools"]

# Tool-arg hygiene: a reflection session's rationale is model-generated free
# text with no other length limit upstream -- caps how much of it lands in
# the DB row and (later) back in a future agent's context.
_MAX_RATIONALE_CHARS = 2000


def register_reflection_tools(registry: ToolRegistry, *, strategies: StrategyStore,
                              queue: ReviewQueue,
                              on_proposed: Optional[Callable[[int], None]] = None,
                              views_for: Callable[[str], list[dict]] | None = None,
                              ) -> None:
    """Registers the one tool a reflection session gets for changing a
    strategy: `propose_strategy_revision`. There is deliberately no
    apply/confirm tool here -- every revision goes through the same human
    Pending-page approval as everything else (spec §④: "无下单、无确认类
    工具").

    `views_for(ticker)` (shadow check-in pilot, spec
    2026-11-06-shadow-checkin-design.md) returns the owner's active views a
    revision of that ticker's strategy must answer to. When given, the tool
    takes a `view_alignment` and refuses a proposal that leaves it out while
    such a view exists; the alignment is prefixed to the rationale so the
    Pending card shows it. None (paper, or the pilot off) leaves the tool and
    its schema exactly as before."""

    def propose_strategy_revision(strategy_id: str, new_yaml: str, rationale: str,
                                  view_alignment: str | None = None) -> str:
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

        # Read once, used for: the no-op check right below, the version
        # comparison further down, the diff, and the row's recorded base
        # (which the applier later compares the file's current text against
        # -- see apply_revision_factory).
        old_yaml = path.read_text()
        if new_yaml == old_yaml:
            return (f"error: invalid strategy revision for '{strategy_id}': "
                    "proposed yaml is identical to the current file -- "
                    "nothing to revise")

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
            doc = parse_strategy_text(strategy_id, new_yaml, authoring=True)
        except StrategyValidationError as exc:
            return f"error: invalid strategy revision for '{strategy_id}': {exc}"

        # The proposed version must move strictly forward from the current
        # file's version, or the strategy_versions audit trail (ordered
        # `version DESC`, see StrategyStore.versions) lies about which
        # revision is current: a proposal that omits `version:` (defaults
        # to 1 -- StrategyDoc) against e.g. a version-7 strategy would
        # otherwise snapshot as version=1 and sort to the bottom of the
        # history. draft_strategy (action_tools.py) silently bumps a stale
        # version for the same reason; here the agent gets an error and the
        # required minimum back instead, so it can correct the proposal
        # within its own tool-call budget rather than the applier silently
        # doing something the agent never asked for. No apply-time bump:
        # the applier writes `new_yaml` verbatim (see apply_revision_factory)
        # -- Finding 1's base-match check guarantees the base didn't move
        # underneath this proposal between now and approval.
        try:
            current_doc = parse_strategy_text(strategy_id, old_yaml)
        except StrategyValidationError:
            # The current file is itself broken (a repair proposal) -- no
            # version to compare against, so just require *some* positive
            # version rather than blocking every repair attempt.
            current_doc = None
        if current_doc is not None:
            if doc.version <= current_doc.version:
                return (f"error: invalid strategy revision for '{strategy_id}': "
                        f"version must be greater than the current version "
                        f"({current_doc.version}); set version: "
                        f"{current_doc.version + 1} or higher")
        elif doc.version <= 0:
            return (f"error: invalid strategy revision for '{strategy_id}': "
                    "version must be a positive integer")

        # Finding 1: a reflection proposal must never move `authorization`
        # or `status` -- this tool's whole premise (spec §④: "无下单、无确认类
        # 工具") is that the reflection agent can change thesis/rules but
        # every resulting order still goes through a human. `authorization:
        # auto` on a soft-rule strategy erases that human -- the review
        # agent that reads the reflection's own proposal would be approving
        # its own orders. Dropping `status: active` is quieter but just as
        # real: StrategyDoc defaults `status` to DRAFT, so an omitted field
        # silently takes the strategy out of sentinel monitoring, stop-losses
        # included, with no error anywhere. Checked against `current_doc`
        # (already parsed above for the version check) so a same-value
        # proposal -- the overwhelmingly common case -- passes untouched.
        if current_doc is not None:
            if (doc.authorization != current_doc.authorization
                    or doc.status != current_doc.status):
                return (f"error: invalid strategy revision for '{strategy_id}': "
                        "reflection proposals cannot change authorization or "
                        "status -- thesis and rules only")
        else:
            # Repair case: the current file doesn't parse, so there is no
            # base authorization/status to diff against. Fail conservative
            # rather than skip the gate: reject `auto` outright (no base
            # means no way to know this wasn't a flip), and require the
            # repair proposal to state `status:` explicitly rather than
            # silently accepting StrategyDoc's DRAFT default -- the same
            # silent-monitoring-stop risk the normal-case check above
            # exists to catch, just without a base to compare it to. `raw`
            # is guaranteed a dict here: parse_strategy_text (used to build
            # `doc` above) would have raised on anything else, and that
            # error already returned before this point.
            if doc.authorization == Authorization.AUTO:
                return (f"error: invalid strategy revision for '{strategy_id}': "
                        "reflection proposals against an unparseable current "
                        "file cannot set authorization: auto -- no base to "
                        "compare, failing conservative")
            if "status" not in raw:
                return (f"error: invalid strategy revision for '{strategy_id}': "
                        "reflection proposals against an unparseable current "
                        "file must set status: explicitly -- no base to "
                        "compare, failing conservative")

        if views_for is not None:
            alignment, error = _check_alignment(views_for(doc.position.ticker),
                                                view_alignment)
            if error:
                return f"error: invalid strategy revision for '{strategy_id}': {error}"
            if alignment:
                rationale = f"View alignment: {alignment}\n\n{rationale}"[:_MAX_RATIONALE_CHARS]

        diff = "\n".join(difflib.unified_diff(
            old_yaml.splitlines(), new_yaml.splitlines(),
            fromfile=f"{strategy_id} (current)", tofile=f"{strategy_id} (proposed)",
            lineterm=""))
        # is_new=False always: this tool only ever revises an existing,
        # already-`path.exists()`-checked file (see the guard at the top of
        # this function) -- it never proposes a brand-new strategy. Passed
        # explicitly (not left to the default) so a repair proposal against
        # a 0-byte/unparseable file -- which also has `old_yaml == ""` --
        # is recorded with the correct is_new flag rather than relying on
        # the now-retired old_yaml=="" sentinel (see add_strategy_revision's
        # docstring for why that sentinel was ambiguous).
        rid = queue.add_strategy_revision(
            strategy_id=strategy_id, ticker=doc.position.ticker,
            old_yaml=old_yaml, new_yaml=new_yaml, diff=diff, rationale=rationale,
            is_new=False)
        if on_proposed is not None:
            on_proposed(int(rid))
        return (f"Revision queued for user approval (#{rid}). It will not "
                "take effect unless approved.")

    t = "string"
    properties: dict = {"strategy_id": {"type": t}, "new_yaml": {"type": t},
                        "rationale": {"type": t}}
    if views_for is not None:
        properties["view_alignment"] = {
            "type": t,
            "description": "Required when the owner has an active view on this "
                           "ticker or on MARKET: 'agree: ...', 'extend: ...' or "
                           "'contradict: ...' with one line of why."}
    registry.register(
        "propose_strategy_revision",
        "Propose a revision to an existing strategy's YAML, based on "
        "reflection over recent performance. This queues the change for "
        "the user's approval on the Pending page -- it is never applied "
        "automatically.",
        {"type": "object", "properties": properties,
         "required": ["strategy_id", "new_yaml", "rationale"]},
        propose_strategy_revision)


_ALIGNMENT_LABELS = ("agree", "extend", "contradict")


def _check_alignment(views: list[dict], alignment: str | None) -> tuple[str, str]:
    """(cleaned alignment, error). A proposal with no relevant view may
    still carry an alignment; one with a relevant view must, and it has to
    lead with one of the three labels so the owner can scan the cards."""
    alignment = (alignment or "").strip()[:500]
    if not alignment:
        if not views:
            return "", ""
        ids = ", ".join(f"#{v['id']} {v['subject']}" for v in views)
        return "", (f"the owner has active views on this ({ids}); set "
                    "view_alignment to 'agree: ...', 'extend: ...' or "
                    "'contradict: ...' with one line of why")
    if not alignment.lower().startswith(_ALIGNMENT_LABELS):
        return "", ("view_alignment must start with agree, extend or "
                    "contradict")
    return alignment, ""
