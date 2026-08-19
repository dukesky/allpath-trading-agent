"""A static, hand-maintained USD-per-1M-token price table for the Settings
-> Usage panel's cost ESTIMATE (store/llm_usage.py, web/routes/settings.py).

This is NOT billing data. It is a best-effort table, kept here in code so
it ships with the app rather than requiring a network call on every
Settings page render (the same "no live fetch on a page render" discipline
`web/models_catalog.py` already follows for the model dropdowns) -- check
your provider's dashboard for what you were actually charged.

Covers the models in `web/models_catalog.py`'s `FALLBACK_MODELS` (the
catalog this app ships with today) plus a conservative -- i.e. deliberately
priced HIGH, not low -- default for anything else, so a custom/unknown
model slug still gets an estimate rather than silently reading as free."""

from __future__ import annotations

from decimal import Decimal

# Bump this whenever the table below is hand-updated -- rendered verbatim
# in the Usage panel's honest-estimate note (web/templates/settings.html).
PRICES_UPDATED = "2026-08-18"

# USD per 1,000,000 tokens: (input, output). Keyed by the bare model SLUG
# (no provider prefix) -- see `price_for` for why lookup strips an
# OpenRouter-style "provider/slug" prefix before matching here, so one
# entry covers both the OpenRouter and direct-provider forms of the same
# model (e.g. "anthropic/claude-sonnet-5" and "claude-sonnet-5" both match
# "claude-sonnet-5" below).
_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "claude-sonnet-5": (Decimal(3), Decimal(15)),
    "claude-opus-5": (Decimal(15), Decimal(75)),
    # Both spellings this app's own catalog uses for the same model --
    # models_catalog.py's FALLBACK_MODELS lists "claude-haiku-4.5" under
    # openrouter/anthropic's naming and "claude-haiku-4-5" under the direct
    # Anthropic provider's dash-separated slug convention.
    "claude-haiku-4.5": (Decimal(1), Decimal(5)),
    "claude-haiku-4-5": (Decimal(1), Decimal(5)),
    "gpt-5.2": (Decimal(5), Decimal(15)),
}

# Conservative (deliberately on the HIGH side, so an estimate never
# understates cost) default rate for any model not in `_PRICES` above --
# marked with `is_default=True` by `price_for`/`estimate_cost` so the panel
# can flag which rows are a real lookup vs this fallback. Computed as the
# max input/output price actually present in `_PRICES` (rather than a
# hand-picked number that can silently fall behind as the table grows) --
# a hardcoded (10, 30) once undercut claude-opus-5's real (15, 75), which
# made the "never understates" claim above false for exactly the model most
# likely to need the fallback (a new, presumably-premium model slug).
DEFAULT_PRICE: tuple[Decimal, Decimal] = (
    max(input_price for input_price, _output_price in _PRICES.values()),
    max(output_price for _input_price, output_price in _PRICES.values()),
)


def price_for(model: str) -> tuple[Decimal, Decimal, bool]:
    """`(input_price_per_1m, output_price_per_1m, is_default)` for `model`.
    `is_default` is `True` whenever `model` isn't in `_PRICES` and the
    conservative `DEFAULT_PRICE` was used instead -- the caller uses this to
    mark an estimate row as such rather than presenting a guess as fact.

    The slug is lowercased before lookup -- `_PRICES` keys are all
    lowercase, and a provider can report a model slug in a different case
    (e.g. a settings page selection round-tripped through an API that
    title-cases it) without that being a genuinely different model."""
    slug = model.rsplit("/", 1)[-1].lower() if model else ""
    if slug in _PRICES:
        input_price, output_price = _PRICES[slug]
        return input_price, output_price, False
    return (*DEFAULT_PRICE, True)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[Decimal, bool]:
    """`(estimated USD, is_default_rate)` for `input_tokens`/`output_tokens`
    against `model`'s price. Never raises on a negative/garbage token count
    -- callers only ever pass values summed straight out of `llm_usage`,
    which itself only ever stores what `LLMResponse` reported (0 at worst,
    see that model's own docstring), but this stays defensive rather than
    trusting that invariant transitively."""
    input_price, output_price, is_default = price_for(model)
    cost = ((Decimal(max(0, input_tokens)) * input_price
            + Decimal(max(0, output_tokens)) * output_price) / Decimal(1_000_000))
    return cost, is_default
