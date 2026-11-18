"""Compact cards (COMPACT_CARDS / SHADOW_COMPACT_CARDS).

The classic review card lists item number, proposal, strategy, price, size,
the brief, the verdict and a security footer; a strategy revision shows a
YAML diff. With the flag on for an account its cards are built here
instead, to be read and decided on in a few seconds:

- order review: what and how much on line 1, the agent's verdict on line 2,
  then one sentence of Why and one of Risk (`order_card`);
- notify-only / skipped rule: one plain sentence (`rule_message`);
- strategy revision: a list of what changes, computed from the old and new
  YAML in code (`revision_changes`), plus what approving would do right now
  (`revision_effect`, the sentinel's own evaluator in a dry run) and one
  sentence of Why (`revision_card`).

Nothing here calls a model. Why and Risk come from the stored decision
brief (allpath_trade/card_brief.py, already checked for grounded numbers)
and are cut to one sentence at render time (`one_sentence`); the stored
brief is not changed. Without a brief the Why line falls back to the first
sentence of the text the classic card already shows (the agent's reasoning,
or the chat proposal's reason).

With the flag off nothing here runs. The flag changes the text of cards
only: never routing, approval, the risk gate, or what is stored on a review
besides `pending_reviews.card_format`."""

from __future__ import annotations

import ast
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Self

import yaml

FLAG = "compact_cards"

# What `pending_reviews.card_format` holds for a card built here. NULL means
# the classic card.
FORMAT = "compact"

TARGET_WORDS = 20   # what the prompt asks for
MAX_WORDS = 28      # hard cap at render time
MAX_BULLETS = 6

# Email only (Telegram has the buttons). The classic multi-sentence security
# footer stays on classic cards.
EMAIL_FOOTER = ("Decide in the AllPath dashboard. A link here works once, "
                "for this item only, for 24 hours.")
ORDER_THREAD_HINT = "\n\n↩️ Reply to ask why. Only the buttons approve or reject."
SHADOW_ORDER_LINE = "Shadow: approving only records it; you place the order yourself."


class CardBody(str):
    """A compact card's text. `.email` is the same text plus the one-line
    email footer; `.card_format` is what to record on the review. A `str`
    subclass (like `notify.events.Subject`) so it passes through every
    existing `body: str` parameter; code that rebuilds the string just
    loses the two attributes and treats it as a plain body."""

    email: str
    card_format: str

    def __new__(cls, text: str, *, footer: str = "") -> Self:
        obj = super().__new__(cls, text)
        obj.email = f"{text}\n\n{footer}" if footer else text
        obj.card_format = FORMAT
        return obj


def enabled(queue: object) -> bool:
    """Whether the account behind `queue` has the flag on. app.py sets
    `ReviewQueue.compact_cards` once at startup; anything else (a test
    double, an older caller) counts as off."""
    return getattr(queue, "compact_cards", False) is True


# ---------------------------------------------------------------------------
# One sentence, hard-capped.
# ---------------------------------------------------------------------------

_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "etc.", "approx.", "inc.", "corp.", "co.",
                  "ltd.", "u.s.", "mr.", "ms.", "dr.", "st.", "jan.", "feb.",
                  "mar.", "apr.", "jun.", "jul.", "aug.", "sep.", "sept.", "oct.",
                  "nov.", "dec.")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s+(?=[\"'(\[$]?[A-Z0-9])")
_MARKDOWN = re.compile(r"\*\*+|`+|__+|(?:^|(?<=\s))#+\s")


def one_sentence(text: Any, max_words: int = MAX_WORDS) -> str:
    """The first sentence of `text`, at most `max_words` words. A longer
    sentence is cut at the word limit and ends in an ellipsis, so the reader
    can see it was cut. Never adds a word that was not there."""
    flat = " ".join(_MARKDOWN.sub("", str(text or "")).split())
    if not flat:
        return ""
    end = len(flat)
    for match in _SENTENCE_END.finditer(flat):
        head = flat[:match.start() + 1]
        last_word = head.rsplit(" ", 1)[-1].lower()
        if last_word in _ABBREVIATIONS:
            continue
        end = match.start() + 1
        if flat[end:end + 1] in "\"')]":
            end += 1
        break
    sentence = flat[:end].strip()
    words = sentence.split()
    if len(words) > max_words:
        return " ".join(words[:max_words]).rstrip(",;:—-") + "…"
    return sentence


# ---------------------------------------------------------------------------
# Numbers in plain form.
# ---------------------------------------------------------------------------

def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip().replace(",", "").replace("$", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def money(value: Any) -> str:
    """$1,500 for a whole amount, $135.35 otherwise."""
    number = _decimal(value)
    if number is None:
        return str(value)
    if number == number.to_integral_value():
        return f"${number:,.0f}"
    return f"${number:,.2f}"


def _plain(number: Decimal) -> str:
    text = f"{number:f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def percent(value: Any) -> str:
    """A stored fraction as a percentage: 0.14 -> 14%, 0.125 -> 12.5%."""
    number = _decimal(value)
    if number is None:
        return str(value)
    return f"{_plain(number * 100)}%"


def shares(value: Any) -> str:
    """Share counts rounded the way a person says them (11, 4.5, 0.42,
    0.004), with a leading "~" whenever rounding changed the number."""
    number = _decimal(value)
    if number is None:
        return str(value)
    if number >= 10:
        rounded = number.quantize(Decimal(1))
    elif number >= 1:
        rounded = number.quantize(Decimal("0.1"))
    elif number >= Decimal("0.01"):
        rounded = number.quantize(Decimal("0.01"))
    else:
        rounded = number.quantize(Decimal("0.0001"))
    text = f"{rounded:,f}"
    text = text.rstrip("0").rstrip(".") if "." in text else text
    return text if rounded == number else f"~{text}"


# ---------------------------------------------------------------------------
# Rule conditions in plain words.
# ---------------------------------------------------------------------------

_OPS = {ast.Lt: "<", ast.LtE: "≤", ast.Gt: ">", ast.GtE: "≥", ast.Eq: "="}
_VAR_LABEL = {"price": "price", "position_weight": "weight", "position_qty": "shares",
              "avg_entry_price": "avg cost", "pnl_pct": "P&L",
              "target_weight": "target weight"}


def _number_for(var: str, value: Decimal) -> str:
    if var in ("price", "avg_entry_price"):
        return money(value)
    if var in ("position_weight", "target_weight"):
        return percent(value)
    if var == "pnl_pct":
        return f"{_plain(value)}%"
    return _plain(value)


def _constant(node: ast.AST) -> Decimal | None:
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub)
            and isinstance(node.operand, ast.Constant)):
        inner = _decimal(node.operand.value)
        return None if inner is None else -inner
    if isinstance(node, ast.Constant):
        return _decimal(node.value)
    return None


def _comparisons(text: str) -> list[tuple[str, str, Decimal]] | None:
    """`(variable, operator, number)` for a condition made only of
    `variable <op> number` terms joined by `and`, else None."""
    try:
        body = ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError):
        return None
    nodes = (body.values if isinstance(body, ast.BoolOp) and isinstance(body.op, ast.And)
             else [body])
    out = []
    for node in nodes:
        if not (isinstance(node, ast.Compare) and len(node.ops) == 1
                and isinstance(node.left, ast.Name) and type(node.ops[0]) in _OPS):
            return None
        number = _constant(node.comparators[0])
        if number is None:
            return None
        out.append((node.left.id, _OPS[type(node.ops[0])], number))
    return out


def _operand_text(node: ast.AST, var: str) -> str | None:
    if isinstance(node, ast.Name):
        return _VAR_LABEL.get(node.id, node.id)
    number = _constant(node)
    return None if number is None else _number_for(var, number)


def _plain_node(node: ast.AST) -> str | None:
    if isinstance(node, ast.BoolOp):
        parts = [_plain_node(v) for v in node.values]
        if any(p is None for p in parts):
            return None
        joiner = " and " if isinstance(node.op, ast.And) else " or "
        # A nested and/or group keeps its brackets, whichever way round.
        return joiner.join(f"({p})" if isinstance(v, ast.BoolOp) else p
                           for p, v in zip(parts, node.values, strict=True))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _plain_node(node.operand)
        return None if inner is None else f"not ({inner})"
    if isinstance(node, ast.Compare):
        names = [n.id for n in [node.left, *node.comparators] if isinstance(n, ast.Name)]
        var = names[0] if names else ""
        if (len(node.ops) == 1 and isinstance(node.left, ast.Name)
                and node.left.id in ("position_weight", "position_qty")
                and _constant(node.comparators[0]) == 0):
            # `position_weight > 0` is how a rule says "only while held".
            if isinstance(node.ops[0], ast.Gt):
                return "you hold shares"
            if isinstance(node.ops[0], (ast.Eq, ast.LtE)):
                return "you hold none"
        parts = [_operand_text(node.left, var)]
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            if type(op) not in _OPS:
                return None
            parts += [_OPS[type(op)], _operand_text(comparator, var)]
        return None if any(p is None for p in parts) else " ".join(parts)
    return None


def plain_condition(text: str) -> str:
    """`price <= 20 and position_weight < 0.12` -> `price ≤ $20 and weight
    < 12%`. Anything this cannot read comes back unchanged."""
    try:
        plain = _plain_node(ast.parse(str(text), mode="eval").body)
    except (SyntaxError, ValueError, RecursionError):
        return str(text)
    return plain or str(text)


def _price_direction(condition: str) -> str:
    """"fell to" / "rose to" when the rule is a single price threshold."""
    terms = _comparisons(condition)
    if terms and len(terms) == 1 and terms[0][0] == "price":
        return {"<": "fell to", "≤": "fell to", ">": "rose to", "≥": "rose to"}.get(
            terms[0][1], "is at")
    return "is at"


# ---------------------------------------------------------------------------
# Order review card.
# ---------------------------------------------------------------------------

def order_headline(*, ticker: str, side: str = "", notional: Any = None, qty: Any = None,
                   price: Any = None, action: str = "") -> str:
    """`CRDO · Buy $1,500 (~11 sh @ $135.35)`. Falls back to the stored
    action text when the order's side and size are not known."""
    side_word = str(side or "").capitalize()
    amount, count, at = _decimal(notional), _decimal(qty), _decimal(price)
    if not side_word or (amount is None and count is None):
        what = " ".join(str(action or "").split()) or "order"
        what = what[:1].upper() + what[1:]
        if at is not None and at > 0:
            what += f" (@ {money(at)})"
    elif count is not None:
        what = f"{side_word} {shares(count)} sh"
        if at is not None and at > 0:
            what += f" @ {money(at)} (~{money((count * at).quantize(Decimal(1)))})"
    else:
        what = f"{side_word} {money(amount)}"
        if at is not None and at > 0:
            what += f" ({_approx(shares(amount / at))} sh @ {money(at)})"
    return f"{ticker} · {what}" if ticker else what


def _approx(text: str) -> str:
    return text if text.startswith("~") else f"~{text}"


def _order_subject_what(ticker: str, side: str, notional: Any, qty: Any, action: str) -> str:
    amount, count = _decimal(notional), _decimal(qty)
    if side and amount is not None:
        what = f"{side.lower()} {money(amount)}"
    elif side and count is not None:
        what = f"{side.lower()} {shares(count)} sh"
    else:
        what = one_sentence(action, 8) or "order"
    return f"{ticker} {what}".strip()


def verdict_word(recommendation: str) -> str:
    """The verdict from the "<verdict> — <reasoning excerpt>" string the
    callers build (sentinel.py), or "" when there is none."""
    return str(recommendation or "").split(" — ", 1)[0].strip()


def why_risk(brief: dict | None, fallback: str = "") -> tuple[str, str]:
    """One sentence each. Why falls back to the first sentence of
    `fallback` (the reasoning excerpt or chat reason the classic card
    shows); Risk has no fallback and is left off without a brief."""
    brief = brief or {}
    why = one_sentence(brief.get("why")) or one_sentence(fallback)
    return why, one_sentence(brief.get("risk"))


def order_card(*, account: str, review_id: int, ticker: str, action: str,
               recommendation: str = "", brief: dict | None = None,
               side: str = "", notional: Any = None, qty: Any = None, price: Any = None,
               kind: str = "order", source: str = "", option_preview: str = "",
               no_analysis_reason: str = "", stop_warning: str = "",
               approve_url: str = "", event: dict | None = None,
               prefix: str = "") -> tuple[str, CardBody]:
    """Subject text and body of a compact order card. `source` is "chat"
    for an order proposed in conversation: it has no agent verdict, and its
    `action` is the proposal's free-text reason (used as the Why fallback,
    not as the headline). `event`/`prefix` are the caller's
    (`notify.events.review_queued`), which wraps the subject."""
    del review_id, event  # the card itself does not show the item number
    chat = source == "chat"
    verdict = verdict_word(recommendation)
    excerpt = recommendation.split(" — ", 1)[1] if " — " in recommendation else ""
    headline = order_headline(ticker=ticker, side=side, notional=notional, qty=qty,
                              price=price, action="" if chat else action)
    lines = []
    if no_analysis_reason:
        lines.append(f"⚠ No agent analysis: {no_analysis_reason}. Check this order yourself.")
    if stop_warning:
        lines.append(f"⚠ {stop_warning}")
    lines.append(headline)
    if verdict and not no_analysis_reason:
        lines.append(f"Agent: {verdict.upper()}")
    elif chat:
        lines.append("Proposed in chat")
    if option_preview:
        lines.append(f"Option at trigger: {option_preview} (re-priced when you approve)")
    why, risk = why_risk(brief, action if chat else excerpt)
    if why or risk:
        lines.append("")
        if why:
            lines.append(f"Why: {why}")
        if risk:
            lines.append(f"Risk: {risk}")
    tail = []
    if account == "shadow" and kind == "order":
        tail.append(SHADOW_ORDER_LINE)
    if approve_url:
        tail.append(f"Review & approve: {approve_url}")
    if tail:
        lines += ["", *tail]

    what = _order_subject_what(ticker, side, notional, qty, "" if chat else action)
    if no_analysis_reason:
        outcome = "no agent analysis"
    elif verdict:
        outcome = f"agent says {verdict.lower()}"
    else:
        outcome = "waiting for your approval"
    return f"{prefix}{what} — {outcome}", CardBody("\n".join(lines), footer=EMAIL_FOOTER)


# ---------------------------------------------------------------------------
# Notify-only / skipped rule message.
# ---------------------------------------------------------------------------

def rule_message(*, ticker: str, rule_id: str, condition: str, disposition: str,
                 price: Any = None, reason: str = "", prefix: str = "") -> tuple[str, str]:
    """Subject text and body for a rule that fired without leaving anything
    to approve. `disposition` is the sentinel's own word ("notified" for an
    alert-only strategy, "skipped" when nothing was proposed). `reason` is a
    plain clause the caller knows to be true ("you hold no TSLA to sell",
    "the agent decided not to trade: ..."); empty means the code does not
    know why, and the message then gives no reason."""
    at = _decimal(price)
    rule = plain_condition(condition)
    if disposition == "notified":
        where = (f"{ticker} {_price_direction(condition)} {money(at)}"
                 if at is not None else f"{ticker}: your alert {rule_id} triggered")
        body = f"{where} (your alert: {rule}). Nothing to approve."
        subject = (f"{prefix}{ticker} alert: {money(at)} ({rule})" if at is not None
                   else f"{prefix}{ticker} alert: {rule}")
        return subject, body
    at_text = f" at {money(at)}" if at is not None else ""
    fired = f"{ticker}: rule {rule_id} triggered{at_text} ({rule})"
    clause = " ".join(str(reason or "").split()).rstrip(".")
    if clause:
        body = f"{fired}, but no order was made: {clause}. Nothing to approve."
    else:
        body = f"{fired}. No order was made. Nothing to approve."
    return f"{prefix}{ticker}: rule {rule_id} triggered, no order", body


# ---------------------------------------------------------------------------
# Strategy revision: what changes, from the two YAML texts. No model.
# ---------------------------------------------------------------------------

def _mapping(text: str) -> dict | None:
    """A strategy text as a mapping. Through the real loader when it
    validates, so both sides carry the same defaults and spellings (a
    draft's new text is a re-serialised model: upper-case ticker, `rearm`
    in minutes, `max_fires_per_day` filled in) and a hand-written old file
    does not show those as changes. Otherwise the raw YAML mapping."""
    try:
        raw = yaml.safe_load(text or "")
    except yaml.YAMLError:
        return None
    if not isinstance(raw, dict):
        return None
    try:
        from allpath_trade.strategy.loader import parse_strategy_text

        return parse_strategy_text("summary", text).model_dump(mode="json")
    except Exception:  # noqa: BLE001 — not a valid strategy: compare it as written
        return raw


def _same_condition(a: Any, b: Any) -> bool:
    if _same(a, b):
        return True
    try:
        return ast.dump(ast.parse(str(a), mode="eval")) == ast.dump(
            ast.parse(str(b), mode="eval"))
    except (SyntaxError, ValueError):
        return False


def _same(a: Any, b: Any) -> bool:
    """Equal as written, or equal as numbers ('0.10' vs 0.1, '10%' vs 0.1)."""
    if a == b:
        return True
    return _fraction(a) is not None and _fraction(a) == _fraction(b)


def _fraction(value: Any) -> Decimal | None:
    text = str(value).strip() if value is not None else ""
    if text.endswith("%"):
        number = _decimal(text[:-1])
        return None if number is None else number / 100
    return _decimal(value)


def _show(value: Any, *, kind: str = "") -> str:
    if value is None or value == "":
        return "none"
    if kind == "weight":
        number = _fraction(value)
        return str(value) if number is None else percent(number)
    if kind == "value":
        return money(value) if _decimal(value) is not None else str(value)
    if isinstance(value, bool):
        return "on" if value else "off"
    return " ".join(str(value).split())


def _rules_by_id(doc: dict) -> dict[str, dict]:
    rules = doc.get("rules")
    out: dict[str, dict] = {}
    for rule in rules if isinstance(rules, list) else []:
        if isinstance(rule, dict) and rule.get("id") is not None:
            out[str(rule["id"])] = rule
    return out


def _rule_brief(rule: dict) -> str:
    action = _show(rule.get("action"))
    condition = plain_condition(str(rule.get("condition", "")))
    return f"{action} when {condition}"


def _condition_change(old: str, new: str) -> str:
    """A threshold move as `trigger weight > 14% → 12%`; anything else as
    the two conditions in plain form."""
    before, after = _comparisons(old), _comparisons(new)
    if (before and after and len(before) == len(after)
            and all(b[:2] == a[:2] for b, a in zip(before, after, strict=True))):
        moved = [f"{_VAR_LABEL.get(b[0], b[0])} {b[1]} {_number_for(b[0], b[2])} → "
                 f"{_number_for(a[0], a[2])}"
                 for b, a in zip(before, after, strict=True) if b[2] != a[2]]
        if moved:
            return "trigger " + ", ".join(moved)
    return f"trigger {plain_condition(old)} → {plain_condition(new)}"


class Change(str):
    """One line of a change list. `key` marks a change the owner must not
    miss (authorization, status, a rule removed, added, turned on or off or
    given a different action, a position limit): those are listed first
    and are never cut by the bullet cap."""

    key: bool

    def __new__(cls, text: str, *, key: bool = False) -> Self:
        obj = super().__new__(cls, text)
        obj.key = key
        return obj


def _minutes(value: Any) -> Any:
    """A rule's `rearm` in minutes, read the way strategy/model.py does
    (60, "60m", "2h"); anything else comes back as written. A draft is
    re-serialised with minutes, so "24h" and 1440 must compare equal."""
    if value is None or isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    try:
        if text.endswith("h"):
            return int(text[:-1]) * 60
        return int(text[:-1]) if text.endswith("m") else int(text)
    except ValueError:
        return value


def _duration(value: Any) -> str:
    minutes = _minutes(value)
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        return _show(value)
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


_STATE_CHANGE = {("armed", "disabled"): "turned off", ("triggered", "disabled"): "turned off",
                 ("disabled", "armed"): "turned on", ("triggered", "armed"): "re-armed",
                 ("armed", "triggered"): "marked as already triggered",
                 ("disabled", "triggered"): "marked as already triggered"}


def _rule_changes(rule_id: str, old: dict, new: dict) -> list[str]:
    parts = []
    before, after = str(old.get("state", "armed")), str(new.get("state", "armed"))
    if before != after:
        parts.append(_STATE_CHANGE.get((before, after), f"state {before} → {after}"))
    if not _same_condition(old.get("condition"), new.get("condition")):
        parts.append(_condition_change(str(old.get("condition", "")),
                                       str(new.get("condition", ""))))
    if not _same(old.get("action"), new.get("action")):
        parts.append(f"action {_show(old.get('action'))} → {_show(new.get('action'))}")
    if not _same(old.get("type"), new.get("type")):
        parts.append(f"type {_show(old.get('type'))} → {_show(new.get('type'))}")
    if _minutes(old.get("rearm")) != _minutes(new.get("rearm")):
        parts.append(f"re-arm {_duration(old.get('rearm'))} → {_duration(new.get('rearm'))}")
    if not _same(old.get("max_fires_per_day"), new.get("max_fires_per_day")):
        parts.append(f"max fires per day {_show(old.get('max_fires_per_day'))} → "
                     f"{_show(new.get('max_fires_per_day'))}")
    known = {"id", "state", "condition", "action", "type", "rearm", "max_fires_per_day"}
    for key in sorted((set(old) | set(new)) - known, key=str):
        if not _same(old.get(key), new.get(key)):
            parts.append(f"{key} {_show(old.get(key))} → {_show(new.get(key))}")
    # Turning a rule on or off, or changing what it does, is a key change.
    return [Change(f"{rule_id}: {part}",
                   key=part in _STATE_CHANGE.values() or part.startswith(("state ", "action ")))
            for part in parts]


_AUTH_NOTE = {"auto": " (hard rules place orders without asking)",
              "confirm": " (every order asks you first)",
              "notify": " (alerts only, no orders)"}
_POSITION_FIELDS = (("target_weight", "target weight", "weight"),
                    ("max_weight", "max weight", "weight"),
                    ("target_value", "target value", "value"),
                    ("max_value", "max value", "value"))
_PLAIN_FIELDS = (("name", "name"), ("horizon", "horizon"), ("bias", "bias"))


def revision_changes(old_yaml: str, new_yaml: str, *, is_new: bool = False) -> list[str] | None:
    """Every change between two strategy YAML texts, one plain line each,
    most consequential first: authorization and status, then rules (added,
    removed, turned on/off, each changed field as old → new), position
    limits, thesis and the rest. Compares the parsed documents, so layout
    (block or one-line flow style, quoting, key order) does not matter.
    `version` is left out: every revision bumps it.

    None when the new text is not a YAML mapping (nothing can be said);
    an unreadable old text is treated like a new strategy."""
    new = _mapping(new_yaml)
    if new is None:
        return None
    old = None if is_new else _mapping(old_yaml)
    if old is None:
        position = new.get("position") if isinstance(new.get("position"), dict) else {}
        auth = str(new.get("authorization", "confirm"))
        lines = [f"authorization: {auth}{_AUTH_NOTE.get(auth, '')}",
                 f"status after approval: {_show(new.get('status', 'draft'))}"]
        for key, label, kind in _POSITION_FIELDS:
            if position.get(key) is not None:
                lines.append(f"{label}: {_show(position[key], kind=kind)}")
        lines += [f"rule {rule_id}: {_rule_brief(rule)}"
                  for rule_id, rule in _rules_by_id(new).items()]
        return lines

    lines = []
    if not _same(old.get("authorization", "confirm"), new.get("authorization", "confirm")):
        after = str(new.get("authorization", "confirm"))
        lines.append(Change(f"authorization: {_show(old.get('authorization', 'confirm'))} → "
                            f"{after}{_AUTH_NOTE.get(after, '')}", key=True))
    if not _same(old.get("status", "draft"), new.get("status", "draft")):
        lines.append(Change(f"status: {_show(old.get('status', 'draft'))} → "
                            f"{_show(new.get('status', 'draft'))}", key=True))

    old_rules, new_rules = _rules_by_id(old), _rules_by_id(new)
    for rule_id, rule in new_rules.items():
        if rule_id not in old_rules:
            lines.append(Change(f"{rule_id}: new rule, {_rule_brief(rule)}", key=True))
        else:
            lines += _rule_changes(rule_id, old_rules[rule_id], rule)
    lines += [Change(f"{rule_id}: removed", key=True)
              for rule_id in old_rules if rule_id not in new_rules]

    old_pos = old.get("position") if isinstance(old.get("position"), dict) else {}
    new_pos = new.get("position") if isinstance(new.get("position"), dict) else {}
    if not _same(old_pos.get("ticker"), new_pos.get("ticker")):
        lines.append(Change(f"ticker: {_show(old_pos.get('ticker'))} → "
                            f"{_show(new_pos.get('ticker'))}", key=True))
    for key, label, kind in _POSITION_FIELDS:
        if not _same(old_pos.get(key), new_pos.get(key)):
            lines.append(Change(f"{label}: {_show(old_pos.get(key), kind=kind)} → "
                                f"{_show(new_pos.get(key), kind=kind)}", key=True))
    if " ".join(str(old.get("thesis", "")).split()) != " ".join(
            str(new.get("thesis", "")).split()):
        lines.append("thesis rewritten")
    for key, label in _PLAIN_FIELDS:
        if not _same(old.get(key), new.get(key)):
            lines.append(f"{label}: {_show(old.get(key))} → {_show(new.get(key))}")
    old_review = old.get("review") if isinstance(old.get("review"), dict) else {}
    new_review = new.get("review") if isinstance(new.get("review"), dict) else {}
    if not _same(old_review.get("cadence", "daily"), new_review.get("cadence", "daily")):
        lines.append(f"review cadence: {_show(old_review.get('cadence', 'daily'))} → "
                     f"{_show(new_review.get('cadence', 'daily'))}")
    if " ".join(str(old_review.get("invalidation", "")).split()) != " ".join(
            str(new_review.get("invalidation", "")).split()):
        lines.append("invalidation text rewritten")
    if not _same(old.get("notify_email", True), new.get("notify_email", True)):
        lines.append(f"email notifications: {_show(bool(old.get('notify_email', True)))} → "
                     f"{_show(bool(new.get('notify_email', True)))}")
    handled = {"authorization", "status", "rules", "position", "thesis", "review",
               "notify_email", "version", "id", *(key for key, _label in _PLAIN_FIELDS)}
    for key in sorted((set(old) | set(new)) - handled, key=str):
        if not _same(old.get(key), new.get(key)):
            lines.append(f"{key} changed")
    # Key changes first (stable), so the cap can only cut the minor ones.
    return ([line for line in lines if getattr(line, "key", False)]
            + [line for line in lines if not getattr(line, "key", False)])


def capped(changes: list[str], limit: int = MAX_BULLETS) -> tuple[list[str], int]:
    """The first `limit` changes and how many were left off. Key changes
    (`Change.key`, listed first) are all kept even past the limit."""
    keep = max(limit, sum(1 for change in changes if getattr(change, "key", False)))
    return changes[:keep], max(0, len(changes) - keep)


def revision_title(*, ticker: str, strategy_id: str, is_new: bool) -> str:
    what = "new strategy" if is_new else "strategy change"
    return " · ".join(part for part in (ticker, strategy_id, what) if part)


def changes_header(count: int, *, is_new: bool) -> str:
    if is_new:
        return "Agent proposes a new strategy:"
    if count == 0:
        return "Agent proposes no change to the rules or settings."
    return f"Agent proposes {count} change{'' if count == 1 else 's'}:"


def revision_card(*, account: str, review_id: int, ticker: str, strategy_id: str,
                  old_yaml: str, new_yaml: str, is_new: bool = False,
                  rationale: str = "", brief: dict | None = None, effect: str = "",
                  approve_url: str = "", web_base_url: str = "",
                  prefix: str = "") -> tuple[str, CardBody]:
    """Subject text and body of a compact strategy-revision card. `effect`
    is `revision_effect`'s sentence, or "" to leave the line out."""
    del account
    changes = revision_changes(old_yaml, new_yaml, is_new=is_new)
    lines = [revision_title(ticker=ticker, strategy_id=strategy_id, is_new=is_new)]
    if changes is None:
        lines.append("Agent proposes a change (it could not be summarised; see the diff).")
        count = 0
    else:
        count = len(changes)
        shown, more = capped(changes)
        lines.append(changes_header(count, is_new=is_new))
        lines += [f"• {change}" for change in shown]
        if more:
            lines.append(f"+{more} smaller changes on the web")
    if effect:
        lines.append(f"Effect now: {effect}")
    why, risk = why_risk(brief, rationale)
    if why:
        lines.append(f"Why: {why}")
    if risk:
        lines.append(f"Risk: {risk}")
    lines.append("")
    if approve_url:
        lines.append(f"Full diff & approve: {approve_url}")
    elif web_base_url:
        lines.append(f"Full diff: {web_base_url}/reviews#review-{int(review_id)}")
    else:
        lines.append("Full diff: Pending page in the dashboard.")
    if is_new:
        outcome = "new strategy proposed"
    else:
        outcome = f"{count} strategy change{'' if count == 1 else 's'} proposed"
    subject = f"{prefix}{strategy_id or ticker} — {outcome}"
    return subject, CardBody("\n".join(lines), footer=EMAIL_FOOTER)


# ---------------------------------------------------------------------------
# "Effect now": the sentinel's evaluator, dry run.
# ---------------------------------------------------------------------------

def revision_effect(doc: Any, *, price: Decimal, ctx: dict[str, Decimal],
                    position: Any = None, equity: Decimal | None = None,
                    confirm_only: bool = False) -> str:
    """What the sentinel's next pass would do with the proposed strategy
    `doc` (a parsed StrategyDoc) at `price`: which armed rules are true
    right now, and what each would lead to under the proposed
    authorization. `ctx` is `Sentinel._build_ctx`'s dict; conditions run
    through the real `evaluate_condition`, and with `equity` given each
    stock action through the real `to_order_intent`, so a sell rule with
    nothing to sell reads "no order". Reads only. Raises on a condition or
    action that cannot be evaluated (the caller then leaves the line out)."""
    from allpath_trade.strategy.actions import is_option_action, parse_action, to_order_intent
    from allpath_trade.strategy.conditions import evaluate_condition
    from allpath_trade.strategy.model import (
        Authorization,
        RuleState,
        RuleType,
        StrategyStatus,
    )

    if confirm_only and doc.authorization == Authorization.AUTO:
        # The paper regime demotes auto to confirm for the pass (run_once).
        doc = doc.model_copy(update={"authorization": Authorization.CONFIRM})
    if doc.status != StrategyStatus.ACTIVE:
        return (f"nothing, the strategy would be {doc.status.value} "
                "(rules are only checked while it is active).")
    firing = [rule for rule in doc.rules
              if rule.state == RuleState.ARMED and evaluate_condition(rule.condition, ctx)]
    if not firing:
        return f"no armed rule fires at the current price ({money(price)})."
    parts = []
    for rule in firing:
        spec = parse_action(rule.action)
        if is_option_action(spec):
            # Option rules depend on market hours, the options backend and
            # contract selection, none of which a dry run can know.
            parts.append(f"{rule.id} is true now (option rule: {rule.action}; "
                         "what it does depends on market hours)")
            continue
        no_order = (equity is not None
                    and to_order_intent(spec, strategy=doc, rule_id=rule.id, price=price,
                                        position=position, equity=equity,
                                        reason="dry run") is None)
        if no_order:
            outcome = "makes no order (nothing to trade)"
        elif doc.authorization == Authorization.NOTIFY:
            outcome = "sends an alert, no order"
        elif doc.authorization == Authorization.AUTO and rule.type == RuleType.HARD:
            outcome = f"runs without asking: {rule.action}"
        elif doc.authorization == Authorization.AUTO:
            outcome = f"goes to the agent to decide: {rule.action}"
        else:
            outcome = f"asks you to approve: {rule.action}"
        parts.append(f"{rule.id} fires and {outcome}")
    return (f"at the current price ({money(price)}) the next check: "
            + "; ".join(parts) + ".")


# ---------------------------------------------------------------------------
# Pending page.
# ---------------------------------------------------------------------------

def web_view(item: dict, *, effect: str = "") -> dict | None:
    """What `_review_card_compact.html` shows first for a decorated review
    row (web/routes/reviews.py's `_decorate`): the same lines as the pushed
    card. None for a kind that has no compact card (a shadow ledger edit),
    which then renders with the classic template."""
    kind = item.get("kind")
    brief = {str(label).lower(): text for label, text in item.get("decision_brief") or []}
    snapshot = item.get("snapshot") if isinstance(item.get("snapshot"), dict) else {}
    if kind == "strategy_revision":
        is_new = bool(snapshot.get("is_new", snapshot.get("old_yaml", "") == ""))
        changes = revision_changes(snapshot.get("old_yaml", ""),
                                   snapshot.get("new_yaml", ""), is_new=is_new)
        shown, more = capped(changes or [])
        why, risk = why_risk(brief, snapshot.get("rationale", ""))
        return {"title": revision_title(ticker=item.get("ticker", ""),
                                        strategy_id=item.get("strategy_id", ""),
                                        is_new=is_new),
                "header": (changes_header(len(changes), is_new=is_new)
                           if changes is not None else
                           "Agent proposes a change (it could not be summarised; "
                           "see the diff)."),
                "changes": shown, "more": more, "effect": effect,
                "why": why, "risk": risk}
    if kind not in ("order", "option_order"):
        return None
    intent = item.get("intent") if isinstance(item.get("intent"), dict) else {}
    analysis = item.get("agent_analysis") if isinstance(item.get("agent_analysis"), dict) else {}
    chat = item.get("source") == "chat"
    stock = kind == "order"
    verdict = ""
    if analysis and not item.get("analysis_unparseable"):
        verdict = str(analysis.get("recommendation") or "").upper()
    why, risk = why_risk(brief, item.get("action", "") if chat
                         else ("" if item.get("analysis_unparseable")
                               else analysis.get("reasoning", "")))
    return {"title": order_headline(
                ticker=item.get("ticker", ""),
                side=intent.get("side", "") if stock else "",
                notional=intent.get("notional") if stock else None,
                qty=intent.get("qty") if stock else None,
                price=snapshot.get("price") if stock else None,
                action="" if chat else item.get("action", "")),
            "verdict": verdict, "chat": chat, "why": why, "risk": risk,
            "plan": brief.get("plan", "")}
