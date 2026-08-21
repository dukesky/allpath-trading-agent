from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import yaml

from allpath_trade.store.accounts import DEFAULT_ACCOUNT, is_valid_account
from allpath_trade.strategy.loader import StrategyValidationError, load_strategy
from allpath_trade.strategy.model import RuleState, StrategyDoc, StrategyStatus


class StrategyStore:
    """Strategies live as YAML files (source of truth for definitions).
    Runtime rule state and version snapshots live in SQLite — the sentinel
    never rewrites the user's YAML.

    `account` scopes `rule_states` and `strategy_versions` (shadow-dual-
    active T1/T2): the same strategy/rule id can independently arm/trigger
    and version per account. `directory` is passed by the caller as-is and
    untouched here -- the per-account directory split (`strategies/
    {account}/`) is the CALLER's job (see `app.py`/`cli.py` build sites),
    not this constructor's: `directory` genuinely IS the strategies
    directory this store reads/writes, whatever the caller resolved it to
    be, so ids can genuinely collide between two `StrategyStore`s pointed at
    different account subdirectories."""

    def __init__(self, directory: Path, conn: sqlite3.Connection,
                account: str = DEFAULT_ACCOUNT) -> None:
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        self.directory = directory
        self._conn = conn
        self._account = account

    @classmethod
    def for_account(cls, root: Path, conn: sqlite3.Connection,
                   account: str) -> StrategyStore:
        """Construct a StrategyStore rooted at `root/{account}/`, gating on
        `is_valid_account(account)` so the T5 shadow-bundle miss (strategies
        dir resolved but account parameter unvalidated) is impossible.

        Used by both app.py and cli.py to construct the per-account store."""
        if not is_valid_account(account):
            raise ValueError(f"invalid account: {account!r}")
        directory = root / account
        directory.mkdir(parents=True, exist_ok=True)
        return cls(directory, conn, account=account)

    def load_all(self, status: StrategyStatus | None = StrategyStatus.ACTIVE,
                 errors: list[str] | None = None) -> list[StrategyDoc]:
        """Load every strategy YAML in the directory. A single invalid file
        must never take down monitoring for every other strategy, so bad
        files are skipped (and, if `errors` is given, reported there) rather
        than raising."""
        docs = []
        for path in sorted(self.directory.glob("*.yaml")):
            try:
                doc = self._merge_states(load_strategy(path))
            except StrategyValidationError as exc:
                if errors is not None:
                    errors.append(f"{path.name}: {exc}")
                continue
            if status is None or doc.status == status:
                docs.append(doc)
        return docs

    def load(self, strategy_id: str) -> StrategyDoc:
        return self._merge_states(load_strategy(self.directory / f"{strategy_id}.yaml"))

    def _merge_states(self, doc: StrategyDoc) -> StrategyDoc:
        rows = self._conn.execute(
            "SELECT rule_id, state FROM rule_states WHERE strategy_id = ? AND account = ?",
            (doc.id, self._account)).fetchall()
        states = {r["rule_id"]: RuleState(r["state"]) for r in rows}
        for rule in doc.rules:
            if rule.id in states:
                rule.state = states[rule.id]
        return doc

    def set_rule_state(self, strategy_id: str, rule_id: str, state: RuleState) -> None:
        self._conn.execute(
            "INSERT INTO rule_states (account, strategy_id, rule_id, state, updated_ts)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(account, strategy_id, rule_id) DO UPDATE SET"
            " state=excluded.state, updated_ts=excluded.updated_ts",
            (self._account, strategy_id, rule_id, state.value,
             datetime.now(UTC).isoformat()))
        self._conn.commit()

    def rearm(self, strategy_id: str, rule_id: str) -> None:
        self.set_rule_state(strategy_id, rule_id, RuleState.ARMED)

    def snapshot_version(self, doc: StrategyDoc, reason: str) -> None:
        content = yaml.safe_dump(doc.model_dump(mode="json"), allow_unicode=True,
                                 sort_keys=False)
        self._conn.execute(
            "INSERT INTO strategy_versions (account, strategy_id, version, ts,"
            " reason, content) VALUES (?, ?, ?, ?, ?, ?)",
            (self._account, doc.id, doc.version, datetime.now(UTC).isoformat(),
             reason, content))
        self._conn.commit()

    def versions(self, strategy_id: str) -> list[sqlite3.Row]:
        # shadow-dual-active T2 (carried from T1 review): scoped by account
        # -- after the directory split, a same-id strategy can legitimately
        # exist in both `paper` and `shadow`, each with its own version
        # history. Without this filter, one account's snapshot rows would
        # bleed into the other's version list purely because the ids match.
        return list(self._conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id = ? AND account = ?"
            " ORDER BY version DESC, id DESC", (strategy_id, self._account)))

    def not_armed_rules(self, strategy_id: str) -> list:
        """Rules of `strategy_id` whose persisted state is not ARMED
        (TRIGGERED or DISABLED). Finding F2: `apply_revision_factory`
        (agent/reflection_tools.py) writes an approved revision's YAML
        verbatim and never touches `rule_states` -- a rule that fired
        before the revision (state TRIGGERED, monitoring stopped for it)
        stays TRIGGERED after the revision lands, silently, even when the
        revision retargets that exact rule id with a tightened condition.
        This never re-arms anything on its own -- re-arming automatically
        could re-fire a stop against a position that's already been sold --
        it only surfaces the fact so the approve flow (web/routes/
        reviews.py, cli.py) can warn the user to re-arm by hand if the rule
        should fire again. Returns `[]` (rather than raising) when the
        strategy no longer loads -- nothing useful to warn about, and the
        approve flow already reports its own outcome for that case."""
        try:
            doc = self.load(strategy_id)
        except (FileNotFoundError, StrategyValidationError):
            return []
        return [r for r in doc.rules if r.state != RuleState.ARMED]

    def rearm_warning(self, strategy_id: str) -> str:
        """Empty string when every rule of `strategy_id` is armed, else a
        human-readable suffix naming each rule that isn't -- shared wording
        for the CLI (`allpath-trade reviews approve`) and the web approve
        flow so a revision's aftermath reads the same on both surfaces."""
        stuck = self.not_armed_rules(strategy_id)
        if not stuck:
            return ""
        parts = ", ".join(f"{r.id} is still {r.state.value}" for r in stuck)
        pronoun = "it" if len(stuck) == 1 else "them"
        return (f" Note: {parts} -- re-arm {pronoun} on the strategy page "
                f"if {'it' if len(stuck) == 1 else 'they'} should fire again.")
