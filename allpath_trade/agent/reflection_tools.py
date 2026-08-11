from __future__ import annotations

import difflib
from collections.abc import Callable

import yaml

from allpath_trade.agent.tools import ToolRegistry
from allpath_trade.store.reviews import ReviewQueue, RevisionValidationError
from allpath_trade.strategy.loader import (
    StrategyValidationError,
    atomic_write_text,
    is_valid_strategy_id,
    parse_strategy_text,
)
from allpath_trade.strategy.model import Authorization
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
            doc = parse_strategy_text(strategy_id, new_yaml)
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


def apply_revision_factory(store: StrategyStore) -> Callable[[str, str, str], None]:
    """Builds the applier `ReviewQueue.set_revision_applier` calls when a
    strategy_revision row is approved. Lives alongside
    `propose_strategy_revision` (rather than in strategy/) because it is
    that tool's write-side counterpart -- keeping propose+apply together
    beats splitting one paired flow across packages."""

    def apply(strategy_id: str, new_yaml: str, expected_base_yaml: str) -> None:
        # Pre-flight gates, in this order, ALL before any write, ALL
        # raising RevisionValidationError (and only that) so
        # ReviewQueue._approve_revision's rollback-to-pending on this
        # exception stays safe (see the loud comment there -- it only holds
        # if this exception never fires after a write has happened):
        #
        # 1. strategy_id must pass the same path-traversal gate the tool
        #    used at propose time. The applier must not trust that that gate
        #    already ran -- it's reachable on its own (directly in tests
        #    today, and this is exactly the kind of gap a future second
        #    caller could slip through). Reproduced by the reviewer:
        #    apply_fn("../escaped", yaml) wrote OUTSIDE the strategies dir.
        if not is_valid_strategy_id(strategy_id):
            raise RevisionValidationError(f"invalid strategy id {strategy_id!r}")
        path = store.directory / f"{strategy_id}.yaml"
        # 2. the strategy file must still exist. Reproduced by the
        #    reviewer: approving a revision for a since-deleted strategy
        #    resurrected it into sentinel monitoring.
        if not path.exists():
            raise RevisionValidationError(
                f"strategy '{strategy_id}' no longer exists")
        # 3. the file's CURRENT text must match `expected_base_yaml`
        #    (snapshot["old_yaml"], threaded through by
        #    ReviewQueue._approve_revision) byte for byte. This is the
        #    check that actually catches staleness -- spec §④'s named
        #    scenario: two proposals drafted from one base, approve the
        #    first, then approving the second must fail and leave it
        #    pending, not silently revert the first approval. The old
        #    "re-validate against parse_strategy_text(new_yaml)" version of
        #    this comment claimed to cover that scenario while being blind
        #    to it -- parsing the proposal's own (already-valid) yaml can
        #    never fail just because the file moved. Reproduced by the
        #    reviewer: approving a stale sibling proposal reverted an
        #    already-approved stop tightening (price<95 back to price<100).
        #    Deliberately an exact-text comparison, not a
        #    structural/version one: ANY intervening change -- including an
        #    unrelated notify_email toggle -- invalidates the proposal,
        #    which also closes the notify_email-revert hole a diff-based
        #    check would still miss.
        if path.read_text() != expected_base_yaml:
            raise RevisionValidationError(
                "strategy file changed since this proposal was made -- "
                "re-propose from the current version")
        try:
            doc = parse_strategy_text(strategy_id, new_yaml)
        except StrategyValidationError as exc:
            raise RevisionValidationError(str(exc)) from exc
        # 4. Finding 1: authorization/status must not move via a revision --
        #    mirrors propose_strategy_revision's own gate (above, same file)
        #    so an approval reached around that tool (a corrupt/hand-edited
        #    queue row, or a future second caller of this applier) can't
        #    still flip a strategy to `auto` or silently drop `status:
        #    active`. Gate 3 above already proved `expected_base_yaml` is
        #    byte-for-byte the file's current text, so it doubles as the
        #    comparison base here. When it doesn't itself parse (a repair
        #    proposal against an already-broken file) there's no base value
        #    to diff against -- fail conservative the same way the propose-
        #    time gate does: reject `auto` outright, and require the
        #    proposal to set `status:` explicitly. All before the write
        #    below, per this function's pre-flight ordering contract.
        try:
            base_doc = parse_strategy_text(strategy_id, expected_base_yaml)
        except StrategyValidationError:
            base_doc = None
        if base_doc is not None:
            if (doc.authorization != base_doc.authorization
                    or doc.status != base_doc.status):
                raise RevisionValidationError(
                    "reflection proposals cannot change authorization or "
                    "status -- thesis and rules only")
        else:
            if doc.authorization == Authorization.AUTO:
                raise RevisionValidationError(
                    "reflection proposals against an unparseable base "
                    "strategy cannot set authorization: auto -- no base to "
                    "compare, failing conservative")
            raw = yaml.safe_load(new_yaml)
            if not (isinstance(raw, dict) and "status" in raw):
                raise RevisionValidationError(
                    "reflection proposals against an unparseable base "
                    "strategy must set status: explicitly -- no base to "
                    "compare, failing conservative")
        # No version bump here: `new_yaml` is written verbatim. Gate 3 above
        # already guarantees the base didn't move underneath this proposal,
        # and propose_strategy_revision's version check already guaranteed
        # `doc.version` moved strictly forward from that base -- bumping
        # again here would be redundant (and, unlike draft_strategy, there
        # is no interactive user to silently rewrite a version out from
        # under).
        atomic_write_text(path, new_yaml)
        store.snapshot_version(doc, reason="reflection revision approved via web")

    return apply
