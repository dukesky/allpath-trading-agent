"""Builders for the research-export tests: a real schema (via store.db.connect)
filled with hand-written rows, then closed so the code under test opens it
read-only the way it will open the production copy."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from allpath_trade.store.db import connect


def strategy_yaml(sid: str, rules: list[dict], *, authorization: str = "auto",
                  status: str = "active", target_weight: str = "0.05",
                  ticker: str = "NVDA") -> str:
    doc = {
        "id": sid, "name": sid, "status": status, "version": 1,
        "authorization": authorization, "thesis": "t",
        "position": {"ticker": ticker, "target_weight": target_weight},
        "rules": rules,
    }
    return yaml.safe_dump(doc, sort_keys=False)


def rule(rid: str, condition: str, action: str = "buy $1000",
         rtype: str = "hard", state: str = "armed") -> dict:
    return {"id": rid, "type": rtype, "condition": condition, "action": action,
            "state": state}


class DBBuilder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.conn = connect(path)

    def review(self, *, ts: str, account: str = "paper", kind: str = "strategy_revision",
               source: str = "reflection", status: str = "approved",
               resolved_ts: str | None = None, strategy_id: str = "s1",
               rule_id: str = "reflection", ticker: str = "NVDA",
               rule_type: str = "revision", old_yaml: str = "", new_yaml: str = "",
               is_new: bool = False, note: str | None = None,
               execution_result: str | None = None, condition: str = "c",
               action: str = "revise strategy") -> int:
        if kind == "strategy_revision":
            snapshot = json.dumps({"old_yaml": old_yaml, "new_yaml": new_yaml,
                                   "diff": "", "rationale": "r", "is_new": is_new})
        else:
            snapshot = json.dumps({"price": 1})
        cur = self.conn.execute(
            "INSERT INTO pending_reviews (account, ts, strategy_id, rule_id, ticker,"
            " rule_type, condition, action, snapshot, status, resolved_ts,"
            " resolution_note, execution_result, kind, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (account, ts, strategy_id, rule_id, ticker, rule_type, condition, action,
             snapshot, status, resolved_ts, note, execution_result, kind, source))
        self.conn.commit()
        return cur.lastrowid

    def observation(self, *, ts: str, source: str, text: str,
                    account: str = "paper", subject: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO observations (account, ts, source, subject, text)"
            " VALUES (?, ?, ?, ?, ?)", (account, ts, source, subject, text))
        self.conn.commit()

    def report(self, *, date: str, account: str = "paper", status: str = "ok",
               model: str = "m-refl", tokens: int = 0) -> None:
        self.conn.execute(
            "INSERT INTO reports (account, date, body, summary, model, tokens_used,"
            " status, created_at) VALUES (?, ?, 'b', 's', ?, ?, ?, ?)",
            (account, date, model, tokens, status, f"{date}T21:00:00+00:00"))
        self.conn.commit()

    def trade(self, *, ts: str, account: str = "paper", status: str = "filled",
              ticker: str = "NVDA", side: str = "buy", strategy_id: str = "s1",
              broker_order_id: str | None = None, reason: str = "strategy s1 rule r1") -> int:
        cur = self.conn.execute(
            "INSERT INTO trades (account, ts, ticker, side, qty, notional, status,"
            " reason, strategy_id, broker_order_id) VALUES (?, ?, ?, ?, '1', NULL,"
            " ?, ?, ?, ?)",
            (account, ts, ticker, side, status, reason, strategy_id, broker_order_id))
        self.conn.commit()
        return cur.lastrowid

    def version(self, *, ts: str, strategy_id: str, version: int, content: str,
                account: str = "paper", reason: str = "") -> None:
        self.conn.execute(
            "INSERT INTO strategy_versions (account, strategy_id, version, ts, reason,"
            " content) VALUES (?, ?, ?, ?, ?, ?)",
            (account, strategy_id, version, ts, reason, content))
        self.conn.commit()

    def llm_usage(self, *, ts: str, tier: str, model: str, inp: int = 10,
                  out: int = 1, purpose: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO llm_usage (ts, tier, model, input_tokens, output_tokens,"
            " purpose) VALUES (?, ?, ?, ?, ?, ?)",
            (ts, tier, model, inp, out, purpose or tier))
        self.conn.commit()

    def llm_failure(self, *, ts: str, tier: str, model: str = "m",
                    error_class: str = "credits") -> None:
        self.conn.execute(
            "INSERT INTO llm_failures (ts, tier, model, error_class, detail)"
            " VALUES (?, ?, ?, ?, '')", (ts, tier, model, error_class))
        self.conn.commit()

    def regime_event(self, *, ts: str, regime: str, account: str = "paper") -> None:
        self.conn.execute(
            "INSERT INTO regime_events (ts, account, regime, by, note,"
            " strategies_flipped) VALUES (?, ?, ?, 'Tian', '', 0)", (ts, account, regime))
        self.conn.commit()

    def rule_fire(self, *, ts: str, strategy_id: str, rule_id: str,
                  account: str = "paper") -> None:
        self.conn.execute(
            "INSERT INTO rule_fires (account, strategy_id, rule_id, ts)"
            " VALUES (?, ?, ?, ?)", (account, strategy_id, rule_id, ts))
        self.conn.commit()

    def close(self) -> Path:
        self.conn.close()
        return self.path
