"""Decision brief on review cards (CARD_REASON_BRIEF / SHADOW_CARD_REASON_BRIEF).

A review card says which stock, what size, what price, and then a long
analysis; the owner was approving on the first three. With the flag on for
an account, each review also carries a three-line brief right under the
action line: why (what the rule saw, with the number), the main risk, and
how it fits his recorded view or the strategy's thesis. About 40 words.

Where the brief comes from: the same model call that already writes the
long text. The review agent returns it as a `brief` object next to its
`reasoning` (agent/review.py); the chat and reflection agents pass it as
`brief_*` arguments of the tool call that carries their `reason` /
`rationale` (agent/action_tools.py, agent/reflection_tools.py). No second
LLM call, so no extra entry in `llm_usage`.

Grounding: `accept` keeps a brief only if every number in it also appears
in the review's stored fields (the long analysis, the rule condition, the
action, the snapshot, the intent, the owner's views). A brief that is
missing, malformed, too long or ungrounded is dropped and the card keeps
its old layout. The accepted brief is stored on the review
(`pending_reviews.decision_brief`), so Telegram, email, the Pending page,
the approve page and the research exports read the same text.

With the flag off nothing here runs: prompts, tool schemas and card text
are what they were. The brief never resolves a review; only the card
buttons, the Pending page, the approve link and the CLI do."""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

FLAG = "card_reason_brief"

MAX_FIELD_CHARS = 240
MAX_TOTAL_WORDS = 80

# (stored key, card label)
_FIELDS = (("why", "Why"), ("risk", "Risk"), ("plan", "Plan"))
_REQUIRED = ("why", "risk")

_SHAPE = (
    '"why": one sentence on what the rule saw, with the number; '
    '"risk": one sentence on the main risk, or what would make this wrong; '
    '"plan": one sentence on how this fits the owner\'s recorded view or the '
    "strategy's thesis (leave it empty if neither is known)")
_RULES = (
    "About 40 words in total, plain language, no hedging filler. It may only "
    "restate what {long_text} already says: every number in it must also "
    "appear in {long_text} or in the {context}. A brief that breaks this is "
    "dropped.")

# Appended to the review agent's prompt when the flag is on for its account.
REVIEW_PROMPT_ADDENDUM = (
    "\nThe owner decides on this card in a few seconds. Add a \"brief\" object "
    "to the same JSON, next to \"reasoning\": {" + _SHAPE + "}. "
    + _RULES.format(long_text="your reasoning", context="trigger data above")
    + "\n")

_TOOL_RULES = _RULES.format(long_text="your reason/rationale",
                            context="proposal itself")

# Extra optional tool arguments (draft_strategy, propose_order,
# propose_strategy_revision) when the flag is on for the account.
TOOL_PROPERTIES: dict[str, dict[str, str]] = {
    "brief_why": {
        "type": "string",
        "description": "Shown at the top of the approval card. One sentence: "
                       "why this, why now, with the number. " + _TOOL_RULES},
    "brief_risk": {
        "type": "string",
        "description": "One sentence: the main risk, or what would make this wrong."},
    "brief_plan": {
        "type": "string",
        "description": "One sentence: how this fits the owner's recorded view "
                       "or the strategy's thesis. Leave it out if neither is known."},
}

_NUMBER = re.compile(r"(?<![A-Za-z0-9_.])\d+(?:,\d{3})*(?:\.\d+)?")


def _numbers(text: str) -> list[Decimal]:
    found = []
    for token in _NUMBER.findall(text):
        try:
            found.append(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return found


def _rounds_to(source: Decimal, target: Decimal) -> bool:
    """`source` written at `target`'s precision is `target` ("207.30" backs
    "207"; a 0.0834 weight backs "8.3"%)."""
    quantum = Decimal(1).scaleb(target.as_tuple().exponent)
    for value in (source, source * 100):
        try:
            if value.quantize(quantum, rounding=ROUND_HALF_UP) == target:
                return True
        except InvalidOperation:
            continue
    return False


def ungrounded_numbers(brief: dict[str, str], sources: list[str]) -> list[str]:
    """Numbers in the brief that no stored field backs."""
    available = _numbers("\n".join(s for s in sources if s))
    missing = []
    for key, _label in _FIELDS:
        for number in _numbers(brief.get(key, "")):
            if not any(_rounds_to(src, number) for src in available):
                missing.append(str(number))
    return missing


def _clean(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def accept(raw: Any, *, sources: list[str], origin: str) -> tuple[dict[str, str] | None, str]:
    """`(brief, "")` when `raw` is a usable, grounded brief, else
    `(None, why it was dropped)`. `sources` are the review's stored texts
    the brief has to be backed by."""
    if raw is None:
        return None, "no brief"
    if not isinstance(raw, dict):
        return None, "brief is not an object"
    brief = {key: _clean(raw.get(key)) for key, _label in _FIELDS}
    for key in _REQUIRED:
        if not brief[key]:
            return None, f"brief has no {key}"
    if any(len(text) > MAX_FIELD_CHARS for text in brief.values()):
        return None, f"a brief line is over {MAX_FIELD_CHARS} characters"
    words = sum(len(text.split()) for text in brief.values())
    if words > MAX_TOTAL_WORDS:
        return None, f"brief is {words} words (limit {MAX_TOTAL_WORDS})"
    missing = ungrounded_numbers(brief, sources)
    if missing:
        return None, ("brief has numbers the stored analysis does not: "
                      + ", ".join(missing))
    brief["origin"] = origin
    return brief, ""


def from_tool_args(why: Any, risk: Any, plan: Any, *, sources: list[str],
                   origin: str) -> tuple[dict[str, str] | None, str]:
    """`accept` for a brief passed as `brief_*` tool arguments."""
    if not (_clean(why) or _clean(risk) or _clean(plan)):
        return None, "no brief"
    return accept({"why": why, "risk": risk, "plan": plan},
                  sources=sources, origin=origin)


def row_sources(row: Any) -> list[str]:
    """The stored fields of a review a brief may draw on."""
    data = dict(row)
    return [str(data.get(key) or "") for key in
            ("ticker", "condition", "action", "snapshot", "intent", "risk_preview")]


def load(raw: Any) -> dict[str, str] | None:
    """A stored `decision_brief` value back as a dict, or None."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, dict):
        return None
    brief = {key: _clean(raw.get(key)) for key, _label in _FIELDS}
    if not all(brief[key] for key in _REQUIRED):
        return None
    brief["origin"] = _clean(raw.get("origin"))
    return brief


def brief_lines(brief: dict[str, str] | None) -> list[str]:
    """The card lines: "Why: ...", "Risk: ...", and "Plan: ..." when set."""
    if not brief:
        return []
    return [f"{label}: {brief[key]}" for key, label in _FIELDS if brief.get(key)]


def labelled(brief: dict[str, str] | None) -> list[tuple[str, str]]:
    """`(label, text)` pairs for the web templates."""
    if not brief:
        return []
    return [(label, brief[key]) for key, label in _FIELDS if brief.get(key)]


def shown(settings: object, account: str, raw: Any) -> list[tuple[str, str]]:
    """What a web surface renders for a row's stored brief: its labelled
    lines when the flag is on for the row's account, else nothing (so a
    brief stored while the flag was on disappears again when it is turned
    off)."""
    from allpath_trade.config import account_flag

    if not account_flag(settings, FLAG, account):
        return []
    return labelled(load(raw))
