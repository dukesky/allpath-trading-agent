from __future__ import annotations

from collections.abc import Callable

import yaml

from allpath_trade.store.reviews import RevisionValidationError
from allpath_trade.strategy.loader import (
    StrategyValidationError,
    atomic_write_text,
    is_valid_strategy_id,
    parse_strategy_text,
)
from allpath_trade.strategy.model import Authorization
from allpath_trade.strategy.store import StrategyStore

# Lives here (not agent/reflection_tools.py, where it used to live) because
# it is now the strategy write path shared by two proposers -- reflection's
# propose_strategy_revision tool AND chat's draft_strategy (spec
# 2026-08-12-chat-strategy-proposals-design.md §①) -- not something specific
# to reflection anymore. reflection_tools re-exports this name so every
# existing import (cli.py, app.py, tests) keeps working unchanged.


def apply_revision_factory(store: StrategyStore) -> Callable[[str, str, str, str], None]:
    """Builds the applier `ReviewQueue.set_revision_applier` calls when a
    strategy_revision row is approved. Every strategy_revision row --
    whichever tool proposed it -- funnels through this one function, so this
    is the ONLY place that ever writes a strategy YAML file (spec's
    unchanged safety invariant: "agent 无直接写策略文件能力;唯一写路径是
    人工批准后的应用器")."""

    def apply(strategy_id: str, new_yaml: str, expected_base_yaml: str,
              source: str = "reflection") -> None:
        # Fail-closed source allowlist -- same precedent as `approve()`'s
        # kind allowlist in store/reviews.py: an unrecognized `source` must
        # never silently fall through to either guard branch below (neither
        # "frozen" nor "unfrozen" is a safe default for a value nobody
        # asked for).
        if source not in ("reflection", "chat"):
            raise RevisionValidationError(f"unknown proposal source {source!r}")

        # Pre-flight gates, in this order, ALL before any write, ALL raising
        # RevisionValidationError (and only that) so ReviewQueue.
        # _approve_revision's rollback-to-pending on this exception stays
        # safe (see the loud comment there -- it only holds if this
        # exception never fires after a write has happened):
        #
        # 1. strategy_id must pass the same path-traversal gate the
        #    propose-time tool used. The applier must not trust that gate
        #    already ran -- it's reachable on its own (directly in tests,
        #    and now from two proposers instead of one). `is_valid_strategy_id`
        #    forbids slashes/dots-first, so `path` below can never escape
        #    `store.directory` -- no separate "inside strategies dir" check
        #    is needed on top of it.
        if not is_valid_strategy_id(strategy_id):
            raise RevisionValidationError(f"invalid strategy id {strategy_id!r}")
        path = store.directory / f"{strategy_id}.yaml"

        # 2. Base check. `expected_base_yaml == ""` is the established
        #    convention (store/reviews.py `add_strategy_revision`) for "this
        #    proposes a BRAND NEW strategy, not a revision" (spec §②) --
        #    staleness there means the opposite of the modify-path check:
        #    the file must still NOT exist. If it now does, someone (a
        #    second chat draft, a hand-added file) created it after this
        #    proposal was made, so approving it here would clobber that file
        #    -- same staleness semantics as the byte-exact check below, just
        #    inverted. For a modify proposal, the file's CURRENT text must
        #    match `expected_base_yaml` byte for byte -- see the original
        #    Finding-1 comment history in git blame for why this is an exact
        #    text comparison rather than a structural/version one (it also
        #    catches an unrelated field toggled underneath a stale sibling
        #    proposal).
        is_new = expected_base_yaml == ""
        if is_new:
            if path.exists():
                raise RevisionValidationError(
                    f"strategy '{strategy_id}' was created after this "
                    "proposal was made -- re-propose against the "
                    "now-existing file")
        else:
            if not path.exists():
                raise RevisionValidationError(
                    f"strategy '{strategy_id}' no longer exists")
            if path.read_text() != expected_base_yaml:
                raise RevisionValidationError(
                    "strategy file changed since this proposal was made -- "
                    "re-propose from the current version")

        # 3. id unchanged. `parse_strategy_text` (used for full validation
        #    just below) always force-overwrites `raw["id"] = strategy_id`
        #    before validating, which would silently swallow an attempted id
        #    change rather than rejecting it -- so the *declared* id has to
        #    be checked against the raw YAML here, before that override
        #    happens. Mirrors propose_strategy_revision's own gate; kept
        #    here too (defense in depth) because this applier is now
        #    reachable from a second proposer that may not run the same
        #    propose-time checks.
        try:
            raw = yaml.safe_load(new_yaml)
        except yaml.YAMLError as exc:
            raise RevisionValidationError(
                f"invalid strategy revision for '{strategy_id}': "
                f"YAML parse error: {exc}") from exc
        if isinstance(raw, dict) and "id" in raw and raw["id"] != strategy_id:
            raise RevisionValidationError(
                f"cannot change strategy id (proposed id {raw['id']!r})")

        # 4. Full YAML/strategy validation -- same for both sources.
        try:
            doc = parse_strategy_text(strategy_id, new_yaml)
        except StrategyValidationError as exc:
            raise RevisionValidationError(str(exc)) from exc

        # 5. Version monotonic, but only when there is a current parseable
        #    file to compare against (an existing, parseable base). A new
        #    strategy has no "current" version to be greater than, and an
        #    unparseable existing base has no version worth trusting either
        #    -- `base_doc` stays None in both cases and this check is
        #    skipped, same as the propose-time version gate's own repair-case
        #    carve-out.
        base_doc: object | None = None
        if not is_new:
            try:
                base_doc = parse_strategy_text(strategy_id, expected_base_yaml)
            except StrategyValidationError:
                base_doc = None
            if base_doc is not None and doc.version <= base_doc.version:
                raise RevisionValidationError(
                    f"invalid strategy revision for '{strategy_id}': "
                    f"version must be greater than the current version "
                    f"({base_doc.version})")

        # 6. Source-branched freeze (spec §①): reflection is an unattended
        #    agent, so it must never be the thing that flips a strategy to
        #    `authorization: auto` or silently drops `status: active` --
        #    that erases the human from the loop it exists to keep intact.
        #    A chat proposal is the user's own intent ("turn TSM to auto",
        #    "make a new active strategy"), so this freeze is SKIPPED for
        #    `source == "chat"` entirely -- including the unparseable-base/
        #    new-strategy conservative rules below, and including a missing
        #    `status:` on a brand-new strategy (StrategyDoc defaults to
        #    draft, which is fine for chat per spec §②: the user activates
        #    on the strategy page).
        if source == "reflection":
            if base_doc is not None:
                if (doc.authorization != base_doc.authorization
                        or doc.status != base_doc.status):
                    raise RevisionValidationError(
                        "reflection proposals cannot change authorization or "
                        "status -- thesis and rules only")
            else:
                # No trustworthy base to diff against -- either the existing
                # file doesn't parse (a repair proposal) or this is a
                # brand-new strategy (`is_new`; reflection never actually
                # proposes these today, but the guard fails conservative
                # here too rather than assuming it never will). Reject
                # `auto` outright, and require `status:` to be stated
                # explicitly rather than silently accepting StrategyDoc's
                # DRAFT default.
                if doc.authorization == Authorization.AUTO:
                    raise RevisionValidationError(
                        "reflection proposals against an unparseable base "
                        "strategy cannot set authorization: auto -- no base "
                        "to compare, failing conservative")
                if not (isinstance(raw, dict) and "status" in raw):
                    raise RevisionValidationError(
                        "reflection proposals against an unparseable base "
                        "strategy must set status: explicitly -- no base to "
                        "compare, failing conservative")

        # No version bump here: `new_yaml` is written verbatim. Gate 2 above
        # already guarantees the base didn't move underneath this proposal
        # (or, for a new strategy, that nothing already occupies this id),
        # and the version check above already guarantees `doc.version`
        # moved strictly forward when there was a base to compare against.
        atomic_write_text(path, new_yaml)
        reason = ("chat proposal approved via web" if source == "chat"
                   else "reflection revision approved via web")
        store.snapshot_version(doc, reason=reason)

    return apply
