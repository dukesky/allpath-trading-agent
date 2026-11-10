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

Grounding: `accept` keeps a brief only if every figure in it is backed by
the review's stored fields (the long analysis and its sources list, the
rule condition, the action, the snapshot, the intent, the owner's views);
the block comment above `_NUMBER` says exactly what counts. Why and Risk
must both pass or there is no brief and the card keeps its old layout; an
ungrounded Plan line is left off on its own. The accepted brief is stored
on the review (`pending_reviews.decision_brief`), so Telegram, email, the
Pending page, the approve page and the research exports read the same
text. Whatever was dropped is stored next to it with the reason
(`pending_reviews.brief_drop`), so drop rate and causes can be read from
the database.

With the flag off nothing here runs: prompts, tool schemas and card text
are what they were. The brief never resolves a review; only the card
buttons, the Pending page, the approve link and the CLI do."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

FLAG = "card_reason_brief"

MAX_FIELD_CHARS = 240
MAX_TOTAL_WORDS = 80
MAX_RAW_CHARS = 600  # per field of a dropped brief kept as evidence

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
    "dropped. Write figures the way {long_text} writes them; no new counts "
    "or arithmetic.")

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

# ---------------------------------------------------------------------------
# Grounding. A brief must not introduce a figure the stored review does not
# support. What counts as supported (fix/card-brief-drops, after the first
# real brief, review #276, was dropped over a lone "9" nobody could trace):
#
# - a number that appears in the stored texts (`sources`), also when the
#   brief writes it rounded or truncated ("$129" for 129.485), as a percent
#   of a stored fraction ("2.8%" for 0.0281), rounded to the nearest ten
#   from 100 up ("about $130"), or with a magnitude suffix ("$1.5k" for
#   1500, "17M" for "16.8M");
# - a gap the review's own trigger data implies (`derived`, see
#   `row_derived`): the trigger price against the rule's thresholds and the
#   average entry price, and the position weight against its limits, as a
#   difference or a percent difference ("30% under the 185 trigger" at a
#   price of 129.49). Only these few pairs: gaps between arbitrary pairs
#   of stored numbers would back almost any small integer by coincidence;
# - a date that appears in the stored texts, or the review's own date
#   (`today`): "as of 11/9" says nothing the card's timestamp does not.
#
# Not supported, on purpose: tool results that did not make it into the
# stored analysis, and bare counts ("3 days to earnings", "2 rungs") that
# no stored field carries. Those are exactly the figures the check is for.
# ---------------------------------------------------------------------------

_SUFFIX = {"k": 3, "K": 3, "thousand": 3, "M": 6, "MM": 6, "million": 6,
           "B": 9, "bn": 9, "billion": 9}
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_.])(\d+(?:,\d{3})*(?:\.\d+)?)"
    r"(?:\s?(k|K|MM|M|B|bn|thousand|million|billion)\b)?")

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
           "nov", "dec")
_DATE_PATTERNS = (
    # 2026-11-09
    (re.compile(r"(?<![\d-])\d{4}-(\d{1,2})-(\d{1,2})(?![\d-])"), False),
    # 11/9, 11/9/26
    (re.compile(r"(?<![\d/.$])(\d{1,2})/(\d{1,2})(?:/\d{2,4})?(?![\d/])"), False),
    # Nov 9, November 9th, 2026
    (re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
                r"(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+\d{4}\b)?", re.IGNORECASE), True),
)


class _Num(NamedTuple):
    value: Decimal
    quantum: Decimal  # the precision it was written at
    text: str


def _numbers(text: str) -> list[_Num]:
    found = []
    for match in _NUMBER.finditer(text):
        try:
            base = Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
        scale = Decimal(10) ** _SUFFIX.get(match.group(2) or "", 0)
        quantum = Decimal(1).scaleb(base.as_tuple().exponent) * scale
        found.append(_Num(base * scale, quantum, match.group(0).strip()))
    return found


def _dates(text: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """`((month, day), (start, end))` for each date written in `text`."""
    found = []
    for pattern, named in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            month = (_MONTHS.index(match.group(1)[:3].lower()) + 1 if named
                     else int(match.group(1)))
            day = int(match.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                found.append(((month, day), match.span()))
    return found


def _written_as(value: Decimal, target: _Num, *, percent: bool = True) -> bool:
    """`value`, rounded or truncated to the precision `target` was written
    at, is `target`, and within 10% of it (so "$2k" is not 1500). With
    `percent`, a stored fraction also backs its percentage (0.0281 ->
    "2.8"). A whole number from 100 up that ends in 0 may also be a
    rounding to the nearest ten ("about $130")."""
    quanta = [target.quantum]
    if target.quantum == 1 and target.value >= 100 and target.value % 10 == 0:
        quanta.append(Decimal(10))
    candidates = [value]
    if percent and 0 < value < 1:
        candidates.append(value * 100)
    for candidate in candidates:
        if abs(candidate - target.value) * 10 > target.value:
            continue
        for quantum in quanta:
            try:
                scaled = candidate / quantum
                wanted = target.value / quantum
                if wanted in (scaled.to_integral_value(ROUND_HALF_UP),
                              scaled.to_integral_value(ROUND_DOWN)):
                    return True
            except InvalidOperation:
                continue
    return False


def _gaps(a: Decimal, b: Decimal) -> list[Decimal]:
    high, low = max(a, b), min(a, b)
    if high == low:
        return []
    gap = high - low
    return [gap, gap / high * 100, *([gap / low * 100] if low else [])]


def ungrounded(text: str, sources: list[str], *, derived: list[Decimal] | None = None,
               today: list[date] | None = None) -> list[str]:
    """Figures and dates in one brief line that the stored review does not
    back (see the block comment above)."""
    source_text = "\n".join(s for s in sources if s)
    known_dates = {md for md, _span in _dates(source_text)}
    known_dates |= {(d.month, d.day) for d in (today or [])}
    for (month_day, (start, end)) in sorted(_dates(text), key=lambda d: -d[1][0]):
        if month_day in known_dates:
            # A backed date: its digits are not figures to check.
            text = text[:start] + " " * (end - start) + text[end:]
    available = [n.value for n in _numbers(source_text)]
    derived = derived or []
    missing = []
    for number in _numbers(text):
        if any(_written_as(value, number) for value in available):
            continue
        if any(_written_as(value, number, percent=False) for value in derived):
            continue
        missing.append(number.text)
    return missing


def ungrounded_numbers(brief: dict[str, str], sources: list[str], *,
                       derived: list[Decimal] | None = None,
                       today: list[date] | None = None) -> list[str]:
    """`ungrounded` over every line of a brief."""
    return [m for key, _label in _FIELDS
            for m in ungrounded(brief.get(key, ""), sources, derived=derived, today=today)]


def _clean(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def review_dates(now: datetime | None = None) -> list[date]:
    """The review's own date, in New York and in UTC (they differ late in
    the evening)."""
    now = now or datetime.now(UTC)
    return [now.astimezone(ZoneInfo("America/New_York")).date(),
            now.astimezone(UTC).date()]


class Checked(NamedTuple):
    """`brief`: what the card may show, or None. `reason`: "" when the brief
    was kept whole. `drop`: the evidence to store when anything was dropped
    (the raw text, why, which figures), or None."""
    brief: dict[str, str] | None
    reason: str
    drop: dict[str, Any] | None


def _raw_for_record(raw: Any) -> Any:
    if isinstance(raw, dict):
        return {str(k)[:40]: (v[:MAX_RAW_CHARS] if isinstance(v, str)
                              else json.dumps(v, default=str)[:MAX_RAW_CHARS])
                for k, v in list(raw.items())[:8]}
    if raw is None:
        return None
    return (raw if isinstance(raw, str) else json.dumps(raw, default=str))[:MAX_RAW_CHARS]


def _drop(raw: Any, origin: str, reason: str, *, missing: list[str] | None = None,
          kept: bool = False) -> dict[str, Any]:
    return {"reason": reason, "origin": origin, "kept_brief": kept,
            "missing": missing or [], "raw": _raw_for_record(raw),
            "ts": datetime.now(UTC).isoformat()}


def accept(raw: Any, *, sources: list[str], origin: str,
           derived: list[Decimal] | None = None, today: list[date] | None = None) -> Checked:
    """Check a model-written brief against the review's stored texts.

    Why and Risk are required and each has to pass on its own; if either
    fails, there is no brief and the card keeps its old layout. Plan is
    optional, so a Plan line that is ungrounded or over-long is left off
    and the rest is kept. Whatever is dropped comes back in `drop`, for
    `ReviewQueue.record_brief_drop`."""
    today = review_dates() if today is None else today
    if raw is None:
        return Checked(None, "no brief", _drop(raw, origin, "no brief"))
    if not isinstance(raw, dict):
        reason = "brief is not an object"
        return Checked(None, reason, _drop(raw, origin, reason))
    brief = {key: _clean(raw.get(key)) for key, _label in _FIELDS}

    def fail(reason: str, missing: list[str] | None = None) -> Checked:
        return Checked(None, reason, _drop(raw, origin, reason, missing=missing))

    for key in _REQUIRED:
        if not brief[key]:
            return fail(f"brief has no {key}")
        if len(brief[key]) > MAX_FIELD_CHARS:
            return fail(f"the {key} line is over {MAX_FIELD_CHARS} characters")
    required_words = sum(len(brief[key].split()) for key in _REQUIRED)
    if required_words > MAX_TOTAL_WORDS:
        return fail(f"brief is {required_words} words (limit {MAX_TOTAL_WORDS})")
    missing = [m for key in _REQUIRED
               for m in ungrounded(brief[key], sources, derived=derived, today=today)]
    if missing:
        return fail("brief has numbers the stored analysis does not: "
                    + ", ".join(missing), missing)

    partial, plan_missing = "", []
    if brief["plan"]:
        plan_missing = ungrounded(brief["plan"], sources, derived=derived, today=today)
        if plan_missing:
            partial = ("plan line left off: numbers the stored analysis does not "
                       "have: " + ", ".join(plan_missing))
        elif len(brief["plan"]) > MAX_FIELD_CHARS:
            partial = f"plan line left off: over {MAX_FIELD_CHARS} characters"
        elif required_words + len(brief["plan"].split()) > MAX_TOTAL_WORDS:
            partial = f"plan line left off: brief over {MAX_TOTAL_WORDS} words"
        if partial:
            brief["plan"] = ""
    brief["origin"] = origin
    drop = _drop(raw, origin, partial, missing=plan_missing, kept=True) if partial else None
    return Checked(brief, partial, drop)


def from_tool_args(why: Any, risk: Any, plan: Any, *, sources: list[str],
                   origin: str, today: list[date] | None = None) -> Checked:
    """`accept` for a brief passed as `brief_*` tool arguments. Leaving all
    three out is normal for a tool call and is not recorded as a drop."""
    if not (_clean(why) or _clean(risk) or _clean(plan)):
        return Checked(None, "no brief", None)
    return accept({"why": why, "risk": risk, "plan": plan},
                  sources=sources, origin=origin, today=today)


def row_sources(row: Any) -> list[str]:
    """The stored fields of a review a brief may draw on, plus the size the
    card itself computes from them ("Est. size: ~11.58 shares")."""
    data = dict(row)
    texts = [str(data.get(key) or "") for key in
             ("ticker", "strategy_id", "rule_id", "condition", "action", "snapshot",
              "intent", "risk_preview")]
    return [*texts, _est_shares(data)]


def row_derived(row: Any) -> list[Decimal]:
    """Gaps a brief may state because the review's trigger data implies
    them: trigger price vs the rule's thresholds and vs the average entry
    price; position weight vs the rule's and the target's weight limits."""
    data = dict(row)
    snapshot = _as_dict(data.get("snapshot"))

    def dec(key: str) -> Decimal | None:
        try:
            return Decimal(str(snapshot.get(key)))
        except (InvalidOperation, TypeError, ValueError):
            return None

    thresholds = [n.value for n in _numbers(str(data.get("condition") or ""))]
    out: list[Decimal] = []
    price = dec("price")
    if price is not None and price > 0:
        others = {t for t in [*thresholds, dec("avg_entry_price")]
                  if t is not None and t >= 1}
        for other in sorted(others):
            out += _gaps(price, other)
    weight = dec("position_weight")
    if weight is not None and 0 <= weight < 1:
        others = {t for t in [*thresholds, dec("target_weight")]
                  if t is not None and 0 < t < 1}
        for other in sorted(others):
            out += _gaps(weight * 100, other * 100)
    return out


def _as_dict(value: Any) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _est_shares(data: dict) -> str:
    """notional / trigger price, as sentinel.py's `_notify_queued` shows it."""
    try:
        notional = Decimal(str(_as_dict(data.get("intent")).get("notional")))
        price = Decimal(str(_as_dict(data.get("snapshot")).get("price")))
        return f"{notional / price:.2f}" if price > 0 else ""
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        return ""


def analysis_sources(reasoning: str, sources: Any) -> list[str]:
    """The stored analysis: its reasoning and its `sources` list (both are
    in `agent_analysis`)."""
    listed = [str(s) for s in sources] if isinstance(sources, list) else []
    return [reasoning or "", *listed]


def load_drop(raw: Any) -> dict[str, Any] | None:
    """A stored `brief_drop` value back as a dict, or None."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if isinstance(raw, dict) else None


def store(queue: Any, review_id: int, checked: Checked, *, log: str) -> dict[str, str] | None:
    """Put a check's outcome on the review: the accepted brief in
    `decision_brief`, anything dropped (raw text and reason) in
    `brief_drop`, plus one stderr line per drop. Returns the brief the card
    may show. Never raises: the review itself is already queued."""
    import sys

    brief = checked.brief
    if brief:
        try:
            queue.attach_brief(review_id, brief)
        except Exception as exc:  # noqa: BLE001 — display only
            print(f"[{log}] could not store card brief for #{review_id}: {exc}",
                  file=sys.stderr)
            brief = None
    if checked.drop:
        print(f"[{log}] #{review_id}: card brief dropped: {checked.drop['reason']}",
              file=sys.stderr)
    try:
        # None clears an older record (a reanalysis whose brief now passes).
        queue.record_brief_drop(review_id, checked.drop)
    except Exception as exc:  # noqa: BLE001 — evidence only
        print(f"[{log}] could not record card brief drop for #{review_id}: {exc}",
              file=sys.stderr)
    return brief


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
