"""Per-account language for what the agent sends the owner
(AGENT_LANGUAGE / SHADOW_AGENT_LANGUAGE).

`en` (the default) changes nothing anywhere. `zh` makes one account's
agent-authored content Chinese: chat replies, card text, rule messages,
receipts, the daily digest, the check-in, and the Telegram outcome lines
and button labels. Tickers, numbers, rule ids, strategy YAML, memory files
and button callback data are never translated, and the web pages stay
English.

Two kinds of text:
- built in code (cards, rule sentences): `tr` looks the English template up
  in `_ZH`. A template with no entry comes back in English, so a missing
  translation is a visible gap, never a crash;
- written by a model: `language_note` / `LANGUAGE_INSTRUCTION` are added to
  the prompt.

Length limits on card text count words with `str.split()`, which sees a
Chinese sentence as one word. `text_units` counts one CJK character as half
a word (20 words is about 40 characters) and is exactly `len(text.split())`
for text with no CJK in it; `first_sentence` ends a sentence at 。！？.
"""

from __future__ import annotations

import re
from typing import Any, Self

LANGUAGES = ("en", "zh")
DEFAULT_LANGUAGE = "en"


def account_language(settings: object, account: str) -> str:
    """`shadow_agent_language` for shadow when set, else `agent_language`;
    anything that is not a known language reads as `en`. getattr, so a
    partial settings double counts as English."""
    value = None
    if account == "shadow":
        value = getattr(settings, "shadow_agent_language", None)
    if not value:
        value = getattr(settings, "agent_language", DEFAULT_LANGUAGE)
    value = str(value or DEFAULT_LANGUAGE).strip().lower()
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


def of(holder: object) -> str:
    """The language recorded on a queue (app.py sets `ReviewQueue.language`
    once at startup); anything else (a test double) is English."""
    value = getattr(holder, "language", DEFAULT_LANGUAGE)
    return value if value in LANGUAGES else DEFAULT_LANGUAGE


class Localized(str):
    """A notification body that remembers its language (`.lang`), so
    `notify.dispatch` can record it on the review. A `str` subclass, like
    `compact_cards.CardBody`; rebuilding the string drops the attribute."""

    lang: str

    def __new__(cls, text: str, lang: str) -> Self:
        obj = super().__new__(cls, text)
        obj.lang = lang
        return obj


# --- counting -------------------------------------------------------------

_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")
_NON_CJK_RUN = re.compile(
    r"[^\s　-〿＀-￯㐀-䶿一-鿿豈-﫿]+")
_CJK_SENTENCE_END = re.compile(r"[。！？!?](?:[”’」』）)\"']*)|\.(?=\s)")


def has_cjk(text: Any) -> bool:
    return bool(_CJK.search(str(text or "")))


def text_units(text: Any) -> int:
    """Length in "words": `len(text.split())` for text without CJK;
    otherwise each run of non-CJK characters is one word and two CJK
    characters are one word (rounded up)."""
    text = str(text or "")
    cjk = len(_CJK.findall(text))
    if not cjk:
        return len(text.split())
    return len(_NON_CJK_RUN.findall(text)) + (cjk + 1) // 2


def first_sentence(text: str, max_units: int) -> str:
    """For text with CJK in it: the first sentence, cut at `max_units`
    (see `text_units`) with an ellipsis when cut. `text` is already
    flattened to one line by the caller."""
    match = _CJK_SENTENCE_END.search(text)
    sentence = text[:match.end()].strip() if match else text.strip()
    if text_units(sentence) <= max_units:
        return sentence
    cut = len(sentence)
    while cut > 0 and text_units(sentence[:cut]) > max_units:
        cut -= 1
    # do not cut inside a number or a Latin word
    while 0 < cut < len(sentence) and re.match(r"[A-Za-z0-9.$%,]", sentence[cut]) \
            and re.match(r"[A-Za-z0-9.$%,]", sentence[cut - 1]):
        cut -= 1
    return sentence[:cut].rstrip(",;:—-，；：、 ") + "…"


# --- prompts --------------------------------------------------------------

LANGUAGE_NOTE_ZH = """\

Language for this account: write everything the owner reads in Simplified
Chinese (简体中文): chat replies, buy/sell plans, pre-trade advice, the
reason / rationale / brief lines of any proposal or strategy change, check-in
questions, and the reflection report with its SUMMARY. Do this even when a
message to you is in English.
Keep these exactly as they are, never translated: ticker symbols, numbers,
prices and dates, rule ids and strategy ids, strategy YAML (keys and values,
including `thesis`), tool arguments that are not shown to the owner, memory
writes (memory files stay in English), JSON keys and fixed JSON values such
as "execute" / "skip", and the section markers REPORT and SUMMARY.
Write plainly, the way a careful friend who trades would talk; no stiff
translated phrasing.
"""

# One line for a single-shot prompt (review agent, check-in questions).
LANGUAGE_INSTRUCTION_ZH = (
    "\nLanguage: write every free-text value the owner will read (reasoning, "
    "the brief lines, questions) in Simplified Chinese. JSON keys, the "
    '"recommendation" value, tickers, numbers and rule ids stay as they are.\n')


def language_note(lang: str) -> str:
    return LANGUAGE_NOTE_ZH if lang == "zh" else ""


def language_instruction(lang: str) -> str:
    return LANGUAGE_INSTRUCTION_ZH if lang == "zh" else ""


# --- code-built strings ---------------------------------------------------

def tr(lang: str, text: str, /, **kw: Any) -> str:
    """`text` (an English template) in `lang`, formatted with `kw`."""
    template = _ZH.get(text, text) if lang == "zh" else text
    return template.format(**kw) if kw else template


def word(lang: str, text: Any) -> str:
    """A single vocabulary word (a side, a verdict) in `lang`; unknown
    words come back unchanged."""
    text = str(text)
    return _ZH_WORDS.get(text.lower(), text) if lang == "zh" else text


_ZH_WORDS = {
    "buy": "买入", "sell": "卖出",
    "execute": "执行", "skip": "跳过",
    "submitted": "已提交", "not submitted": "未提交",
    "on": "开", "off": "关", "none": "无",
    "price": "价格", "weight": "仓位占比", "shares": "股数", "avg cost": "成本价",
    "p&l": "盈亏", "target weight": "目标占比",
    "max weight": "最大占比", "target value": "目标金额", "max value": "最大金额",
    "name": "名称", "horizon": "周期", "bias": "方向",
    "fell to": "跌到", "rose to": "涨到", "is at": "现价",
    "turned off": "已关闭", "turned on": "已开启", "re-armed": "重新启用",
    "marked as already triggered": "标记为已触发",
    "new strategy": "新策略", "strategy change": "策略修改",
    "ledger": "账本", "paper": "Paper", "shadow": "Shadow",
}

_ZH = {
    # compact_cards: order card
    "{side} {count} sh": "{side} {count} 股",
    " ({count} sh @ {price})": "（{count} 股 @ {price}）",
    "⚠ No agent analysis: {reason}. Check this order yourself.":
        "⚠ 没有 Agent 分析：{reason}。请自己核对这笔订单。",
    "LLM unavailable": "模型不可用",
    "the agent's reply could not be parsed": "Agent 的回复无法解析",
    "Agent: {verdict}": "Agent 建议：{verdict}",
    "Proposed in chat": "聊天中提出",
    "Option at trigger: {preview} (re-priced when you approve)":
        "触发时的期权：{preview}（批准时重新定价）",
    "Why: {text}": "原因：{text}",
    "Risk: {text}": "风险：{text}",
    "Plan: {text}": "计划：{text}",
    "Shadow: approving only records it; you place the order yourself.":
        "Shadow：批准只是记账，订单要你自己在券商下。",
    "Review & approve: {url}": "查看并批准：{url}",
    "no agent analysis": "没有 Agent 分析",
    "agent says {verdict}": "Agent 建议{verdict}",
    "waiting for your approval": "等你批准",
    "Decide in the AllPath dashboard. A link here works once, for this item only, "
    "for 24 hours.": "请在 AllPath 面板里决定。这里的链接只能用一次，仅限本条，24 小时内有效。",
    "\n\n↩️ Reply to ask why. Only the buttons approve or reject.":
        "\n\n↩️ 回复本条可以问原因。只有按钮能批准或拒绝。",
    "\n\n↩️ Reply to this message to ask me about it. "
    "A reply cannot approve or change it; use the buttons.":
        "\n\n↩️ 回复本条可以问我。回复不能批准或修改，请用按钮。",
    # compact_cards: rule messages
    "{ticker} {direction} {price}": "{ticker} {direction} {price}",
    "{ticker}: your alert {rule_id} triggered": "{ticker}：你的提醒 {rule_id} 触发了",
    "{where} (your alert: {rule}). Nothing to approve.":
        "{where}（你的提醒：{rule}）。无需批准。",
    "{prefix}{ticker} alert: {price} ({rule})": "{prefix}{ticker} 提醒：{price}（{rule}）",
    "{prefix}{ticker} alert: {rule}": "{prefix}{ticker} 提醒：{rule}",
    " at {price}": "，价格 {price}",
    "{ticker}: rule {rule_id} triggered{at} ({rule})":
        "{ticker}：规则 {rule_id} 触发{at}（{rule}）",
    "{fired}, but no order was made: {clause}. Nothing to approve.":
        "{fired}，但没有生成订单：{clause}。无需批准。",
    "{fired}. No order was made. Nothing to approve.":
        "{fired}。没有生成订单。无需批准。",
    "{prefix}{ticker}: rule {rule_id} triggered, no order":
        "{prefix}{ticker}：规则 {rule_id} 触发，无订单",
    "you hold no {ticker} to sell": "你没有可卖的 {ticker}",
    "the position is already at or above its target size": "仓位已经达到或超过目标",
    "the agent decided not to trade: {why}": "Agent 决定不交易：{why}",
    "the agent decided not to trade": "Agent 决定不交易",
    # compact_cards: conditions
    " and ": " 且 ", " or ": " 或 ", "not ({inner})": "非（{inner}）",
    "you hold shares": "你有持仓", "you hold none": "你没有持仓",
    # compact_cards: strategy revision
    " (hard rules place orders without asking)": "（硬规则不问你直接下单）",
    " (every order asks you first)": "（每笔订单都先问你）",
    " (alerts only, no orders)": "（只提醒，不下单）",
    "authorization: {auth}{note}": "授权：{auth}{note}",
    "status after approval: {status}": "批准后的状态：{status}",
    "{label}: {value}": "{label}：{value}",
    "rule {rule_id}: {brief}": "规则 {rule_id}：{brief}",
    "{action} when {condition}": "{condition} 时 {action}",
    "authorization: {old} → {new}{note}": "授权：{old} → {new}{note}",
    "status: {old} → {new}": "状态：{old} → {new}",
    "{rule_id}: new rule, {brief}": "{rule_id}：新规则，{brief}",
    "{rule_id}: removed": "{rule_id}：已删除",
    "ticker: {old} → {new}": "代码：{old} → {new}",
    "{label}: {old} → {new}": "{label}：{old} → {new}",
    "thesis rewritten": "逻辑（thesis）已重写",
    "review cadence: {old} → {new}": "复盘频率：{old} → {new}",
    "invalidation text rewritten": "失效条件已重写",
    "email notifications: {old} → {new}": "邮件通知：{old} → {new}",
    "{key} changed": "{key} 已修改",
    "state {old} → {new}": "状态 {old} → {new}",
    "trigger {moved}": "触发条件 {moved}",
    "trigger {old} → {new}": "触发条件 {old} → {new}",
    "action {old} → {new}": "动作 {old} → {new}",
    "type {old} → {new}": "类型 {old} → {new}",
    "re-arm {old} → {new}": "重新启用间隔 {old} → {new}",
    "max fires per day {old} → {new}": "每日最多触发 {old} → {new}",
    "{key} {old} → {new}": "{key} {old} → {new}",
    "{rule_id}: {part}": "{rule_id}：{part}",
    "Agent proposes a new strategy:": "Agent 提议一个新策略：",
    "Agent proposes no change to the rules or settings.": "Agent 提议的修改不涉及规则或设置。",
    "Agent proposes {count} change:": "Agent 提议 {count} 处修改：",
    "Agent proposes {count} changes:": "Agent 提议 {count} 处修改：",
    "⚠ Held for human: {reason}": "⚠ 留给你决定：{reason}",
    "Agent proposes a change (it could not be summarised; see the diff).":
        "Agent 提议一处修改（无法自动概括，请看 diff）。",
    "+{more} smaller changes on the web": "另有 {more} 处小修改，见网页",
    "Effect now: {effect}": "现在批准的效果：{effect}",
    "Full diff & approve: {url}": "完整 diff 并批准：{url}",
    "Full diff: {url}": "完整 diff：{url}",
    "Full diff: Pending page in the dashboard.": "完整 diff：面板的 Pending 页。",
    "new strategy proposed": "提议新策略",
    "{count} strategy change proposed": "提议 {count} 处策略修改",
    "{count} strategy changes proposed": "提议 {count} 处策略修改",
    # compact_cards: effect now
    "nothing, the strategy would be {status} (rules are only checked while it is active).":
        "没有效果，策略状态会是 {status}（只有 active 时才检查规则）。",
    "no armed rule fires at the current price ({price}).":
        "按现价（{price}）没有待触发的规则会触发。",
    "{rule_id} is true now (option rule: {action}; what it does depends on market hours)":
        "{rule_id} 现在成立（期权规则：{action}；实际动作取决于是否开盘）",
    "makes no order (nothing to trade)": "不会下单（没有可交易的）",
    "sends an alert, no order": "只发提醒，不下单",
    "runs without asking: {action}": "不问你直接执行：{action}",
    "goes to the agent to decide: {action}": "交给 Agent 决定：{action}",
    "asks you to approve: {action}": "请你批准：{action}",
    "{rule_id} fires and {outcome}": "{rule_id} 触发，{outcome}",
    "at the current price ({price}) the next check: {parts}.":
        "按现价（{price}），下一次检查：{parts}。",
    # notify/events: classic cards and receipts
    "{prefix}[AllPath] {ticker}: rule {rule_id} triggered":
        "{prefix}[AllPath] {ticker}：规则 {rule_id} 触发",
    "Strategy {strategy_id}, rule {rule_id} triggered on {ticker}.\n"
    "Condition: {condition}\nDisposition: {disposition}.":
        "策略 {strategy_id} 的规则 {rule_id} 在 {ticker} 上触发。\n"
        "条件：{condition}\n处理：{disposition}。",
    "notified": "已提醒", "skipped": "已跳过",
    "\n\nOpen the AllPath Trade dashboard to act on this. "
    "This message never carries your dashboard access token. If "
    "you've opted into approval links under Settings, an item "
    "waiting for you may also include a one-time link scoped to "
    "just that item, valid for 24 hours and dead as soon as it's used.":
        "\n\n请打开 AllPath Trade 面板处理。本消息不含面板访问令牌；如果你在 Settings 里开启了"
        "批准链接，待处理的条目可能带一个一次性链接，仅限该条，24 小时内有效，用过即失效。",
    "\n\nAct on this in the AllPath Trade dashboard. This message never "
    "carries your dashboard access token; an approval link, if present, "
    "works once, for this item only, for 24 hours.":
        "\n\n请在 AllPath Trade 面板处理。本消息不含面板访问令牌；如有批准链接，只能用一次，"
        "仅限本条，24 小时内有效。",
    "{prefix}[AllPath] {noun}: waiting for your approval": "{prefix}[AllPath] {noun}：等你批准",
    "Item #{review_id} is waiting for you.": "第 #{review_id} 条在等你处理。",
    "Proposed: {action} on {ticker}": "提议：{ticker} {action}",
    "Proposed: {action}": "提议：{action}",
    "⚠ No agent analysis: {reason}": "⚠ 没有 Agent 分析：{reason}",
    "Strategy: {strategy_id}": "策略：{strategy_id}",
    "Price at trigger: {price}": "触发时价格：{price}",
    "Est. size: ~{shares} shares at that price": "估算数量：按该价约 {shares} 股",
    "Option order at trigger: {preview} — re-priced and re-checked when you approve":
        "触发时的期权订单：{preview}，批准时会重新定价并复查",
    "The agent recommends: {recommendation}": "Agent 建议：{recommendation}",
    "If approved, you'll place it yourself — approving here "
    "only records it in your shadow ledger.":
        "批准后要你自己去下单，这里的批准只是记到 shadow 账本。",
    "\nReview & approve: {url}": "\n查看并批准：{url}",
    "order recorded — place it yourself": "已记账，请自己下单",
    "order {outcome}": "订单{outcome}",
    "{prefix}[AllPath] {ticker}: {headline}": "{prefix}[AllPath] {ticker}：{headline}",
    ": {side} {qty} {ticker} @ ~${price}": "：{side} {qty} {ticker} @ ~${price}",
    "A {side} order for {ticker} was recorded in your shadow "
    "ledger — place this order in your brokerage now{fill}.\n{detail}":
        "{ticker} 的{side}订单已记入 shadow 账本，请现在去券商下单{fill}。\n{detail}",
    "A {side} order for {ticker} was {outcome}.\n{detail}":
        "{ticker} 的{side}订单{outcome}。\n{detail}",
    "{prefix}[AllPath] Daily summary": "{prefix}[AllPath] 每日小结",
    "Shadow account today: {triggers} rule trigger(s), "
    "{trades} order(s) recorded, "
    "{pending} item(s) still waiting for your approval.":
        "Shadow 账户今天：规则触发 {triggers} 次，记账 {trades} 笔，还有 {pending} 条等你批准。",
    "\nEstimated LLM cost today (all accounts): {cost} (see Settings -> Usage).":
        "\n今天的模型费用估算（所有账户）：{cost}（见 Settings -> Usage）。",
    "{prefix}[AllPath] Daily reflection {date}": "{prefix}[AllPath] 每日复盘 {date}",
    # notify/dispatch + telegram: buttons and outcome lines
    "✅ Approve": "✅ 批准", "❌ Reject": "❌ 拒绝",
    "That review no longer exists.": "这条已经不存在了。",
    "Not found": "未找到",
    "That item is no longer pending, or this button has expired.":
        "这条已经处理过了，或者按钮已过期。",
    "Already resolved": "已处理过",
    "{prefix}Not processed: {error}": "{prefix}未处理：{error}",
    "Not processed": "未处理",
    "{prefix}❌ Rejected #{review_id}.": "{prefix}❌ 已拒绝 #{review_id}。",
    "Rejected": "已拒绝",
    "Ledger change": "账本修改", "Revision": "策略修改",
    "{prefix}⚠️ {noun} #{review_id} failed re-validation "
    "({error}) and stayed pending — reopen from the app.":
        "{prefix}⚠️ {noun} #{review_id} 复核没通过（{error}），仍在待处理，请在 app 里重新打开。",
    "Left pending: re-validation failed": "仍待处理：复核没通过",
    "{prefix}⚠️ Review #{review_id} claimed, but execution failed: {error}":
        "{prefix}⚠️ #{review_id} 已受理，但执行失败：{error}",
    "Execution failed": "执行失败",
    "Revision applied to {strategy_id}.": "修改已应用到 {strategy_id}。",
    "{prefix}✅ Approved #{review_id} — {message}": "{prefix}✅ 已批准 #{review_id}：{message}",
    "Approved": "已批准",
    "{prefix}✅ Approved #{review_id} — {action} applied to the shadow ledger.":
        "{prefix}✅ 已批准 #{review_id}：{action} 已写入 shadow 账本。",
    "{prefix}⚠️ #{review_id} rejected by the risk gate: {reasons}":
        "{prefix}⚠️ #{review_id} 被风控拦下：{reasons}",
    "Blocked by risk gate": "被风控拦下",
    "{prefix}✅ Approved #{review_id} — order recorded in "
    "your shadow ledger. Place it in your brokerage now.":
        "{prefix}✅ 已批准 #{review_id}：订单已记入 shadow 账本，请现在去券商下单。",
    "Recorded": "已记账",
    # checkin
    "[Shadow] Evening check-in ({et_date})": "[Shadow] 晚间 check-in（{et_date}）",
    "Reply to this message -- a line or two is enough. "
    "Skip any you don't care about.": "回复本条即可，一两句就够，不关心的可以跳过。",
    "Your take...": "你的看法…",
}


def register(entries: dict[str, str]) -> None:
    """Add translations from another module (holdings sync, advisor), so
    each feature keeps its own strings next to its code."""
    _ZH.update(entries)


def lang_kw(holder: object) -> dict[str, str]:
    """`{"lang": ...}` for a non-English queue, `{}` for English: spread
    into a builder call so an English account makes exactly the old call."""
    lang = of(holder)
    return {} if lang == DEFAULT_LANGUAGE else {"lang": lang}
