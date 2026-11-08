"""Helpers shared by the case modules."""

from __future__ import annotations

from typing import Any

import yaml

from allpath_trade.agent.loop import AgentSession
from allpath_trade.llm.base import LLMResponse
from allpath_trade.web.chat_service import ChatService
from research.redteam import corpus
from research.redteam.env import FROZEN_NOW, REPORT_TEXT, RedteamEnv, call, say

FIX = corpus.fixtures()

# Locations used by several modules.
UNKNOWN_TOOL = ("allpath_trade/agent/tools.py", 'return f"error: unknown tool')


def revise(base: str, *, drop: str | None = None, remove_rule: str | None = None,
           add_rule: dict | None = None, rule_condition: tuple[str, str] | None = None,
           **fields: Any) -> str:
    """A YAML revision of `base`: top-level `fields` overwritten (`ticker`
    goes into `position`), one key dropped, one rule removed or added."""
    doc = yaml.safe_load(base)
    for key, value in fields.items():
        if key == "ticker":
            doc["position"]["ticker"] = value
        else:
            doc[key] = value
    if drop:
        doc.pop(drop, None)
    if remove_rule:
        doc["rules"] = [r for r in doc["rules"] if r["id"] != remove_rule]
    if add_rule:
        doc["rules"].append(dict(add_rule))
    if rule_condition:
        rid, cond = rule_condition
        for r in doc["rules"]:
            if r["id"] == rid:
                r["condition"] = cond
    return yaml.safe_dump(doc, sort_keys=False)


def run_reflection(env: RedteamEnv, *steps: LLMResponse, account: str = "paper") -> str:
    """Run the real nightly Reflector with a scripted model. `steps` are the
    model's tool calls; the required REPORT is appended (twice, covering the
    corrective turn)."""
    env.script("memory", *steps, say(REPORT_TEXT), say(REPORT_TEXT))
    return env.c.accounts[account].reflector.run_daily(now=FROZEN_NOW)


def propose(strategy_id: str, new_yaml: str, rationale: str = "redteam",
            id_: str = "p1", **extra: Any) -> LLMResponse:
    return call("propose_strategy_revision",
                {"strategy_id": strategy_id, "new_yaml": new_yaml,
                 "rationale": rationale, **extra}, id_)


class Holder:
    def __init__(self, c: Any) -> None:
        self.c = c

    def get(self) -> Any:
        return self.c


def chat_service(env: RedteamEnv, account: str = "paper") -> ChatService:
    return ChatService(Holder(env.c), account=account)


def chat_turn(env: RedteamEnv, text: str, *steps: LLMResponse,
              account: str = "paper", service: ChatService | None = None) -> str:
    """One real web-chat turn (ChatService.send) with a scripted model."""
    env.script("chat", *steps, say("done"))
    return (service or chat_service(env, account)).send(text, source="web")


def registry_of(session: AgentSession):
    return session.registry


def tool_names(service: ChatService) -> set[str]:
    return {s.name for s in service.session().registry.specs()}


def pending(env: RedteamEnv, account: str = "paper") -> list[dict]:
    return [dict(r) for r in env.c.accounts[account].queue.list("pending")]


def journal_rows(env: RedteamEnv, account: str = "paper") -> list[dict]:
    return [dict(r) for r in env.c.conn.execute(
        "SELECT * FROM trades WHERE account = ? ORDER BY id", (account,))]


def first(results: list[str], prefix: str) -> str:
    for r in results:
        if r.startswith(prefix):
            return r
    return ""
