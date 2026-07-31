from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import yaml

from tradewind.strategy.loader import load_strategy
from tradewind.strategy.model import RuleState, StrategyDoc, StrategyStatus


class StrategyStore:
    """Strategies live as YAML files (source of truth for definitions).
    Runtime rule state and version snapshots live in SQLite — the sentinel
    never rewrites the user's YAML."""

    def __init__(self, directory: Path, conn: sqlite3.Connection) -> None:
        self.directory = directory
        self._conn = conn

    def load_all(self, status: StrategyStatus | None = StrategyStatus.ACTIVE
                 ) -> list[StrategyDoc]:
        docs = []
        for path in sorted(self.directory.glob("*.yaml")):
            doc = self._merge_states(load_strategy(path))
            if status is None or doc.status == status:
                docs.append(doc)
        return docs

    def load(self, strategy_id: str) -> StrategyDoc:
        return self._merge_states(load_strategy(self.directory / f"{strategy_id}.yaml"))

    def _merge_states(self, doc: StrategyDoc) -> StrategyDoc:
        rows = self._conn.execute(
            "SELECT rule_id, state FROM rule_states WHERE strategy_id = ?",
            (doc.id,)).fetchall()
        states = {r["rule_id"]: RuleState(r["state"]) for r in rows}
        for rule in doc.rules:
            if rule.id in states:
                rule.state = states[rule.id]
        return doc

    def set_rule_state(self, strategy_id: str, rule_id: str, state: RuleState) -> None:
        self._conn.execute(
            "INSERT INTO rule_states (strategy_id, rule_id, state, updated_ts)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(strategy_id, rule_id) DO UPDATE SET state=excluded.state,"
            " updated_ts=excluded.updated_ts",
            (strategy_id, rule_id, state.value,
             datetime.now(UTC).isoformat()))
        self._conn.commit()

    def rearm(self, strategy_id: str, rule_id: str) -> None:
        self.set_rule_state(strategy_id, rule_id, RuleState.ARMED)

    def snapshot_version(self, doc: StrategyDoc, reason: str) -> None:
        content = yaml.safe_dump(doc.model_dump(mode="json"), allow_unicode=True,
                                 sort_keys=False)
        self._conn.execute(
            "INSERT INTO strategy_versions (strategy_id, version, ts, reason, content)"
            " VALUES (?, ?, ?, ?, ?)",
            (doc.id, doc.version, datetime.now(UTC).isoformat(),
             reason, content))
        self._conn.commit()

    def versions(self, strategy_id: str) -> list[sqlite3.Row]:
        return list(self._conn.execute(
            "SELECT * FROM strategy_versions WHERE strategy_id = ?"
            " ORDER BY version DESC", (strategy_id,)))
