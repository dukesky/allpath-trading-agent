from decimal import Decimal

from allpath_trade.llm import prices


def test_known_model_returns_its_own_price_not_default():
    input_price, output_price, is_default = prices.price_for("claude-sonnet-5")
    assert is_default is False
    assert input_price == Decimal(3)
    assert output_price == Decimal(15)


def test_openrouter_prefixed_slug_matches_the_same_entry():
    a = prices.price_for("claude-sonnet-5")
    b = prices.price_for("anthropic/claude-sonnet-5")
    assert a == b


def test_unknown_model_falls_back_to_conservative_default():
    input_price, output_price, is_default = prices.price_for("some-brand-new-model-x")
    assert is_default is True
    assert (input_price, output_price) == prices.DEFAULT_PRICE


def test_empty_model_string_falls_back_to_default():
    _input_price, _output_price, is_default = prices.price_for("")
    assert is_default is True


def test_estimate_cost_known_model_math():
    # claude-sonnet-5: $3/1M in, $15/1M out.
    cost, is_default = prices.estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000)
    assert is_default is False
    assert cost == Decimal(3) + Decimal(15)


def test_estimate_cost_unknown_model_uses_default_and_flags_it():
    cost, is_default = prices.estimate_cost("mystery-model", 1_000_000, 0)
    assert is_default is True
    assert cost == prices.DEFAULT_PRICE[0]


def test_estimate_cost_zero_tokens_is_zero():
    cost, _is_default = prices.estimate_cost("claude-sonnet-5", 0, 0)
    assert cost == Decimal(0)


def test_estimate_cost_never_raises_on_negative_tokens():
    cost, _is_default = prices.estimate_cost("claude-sonnet-5", -5, -5)
    assert cost == Decimal(0)


def test_both_haiku_spellings_are_priced_identically():
    dot = prices.price_for("claude-haiku-4.5")
    dash = prices.price_for("claude-haiku-4-5")
    assert dot == dash
    assert dot[2] is False  # both are real lookups, not the default
