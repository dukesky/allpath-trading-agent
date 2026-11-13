"""Every strategy template under docs/examples/ must load with the real
parser, in both modes: the plain load the sentinel does, and the stricter
authoring check a chat draft goes through."""

from __future__ import annotations

from pathlib import Path

import pytest

from allpath_trade.strategy.loader import (
    is_valid_strategy_id,
    load_strategy,
    parse_strategy_text,
)
from allpath_trade.strategy.model import Authorization, RuleState, StrategyStatus

EXAMPLES = Path(__file__).resolve().parent.parent / "docs" / "examples"
TEMPLATES = sorted((EXAMPLES / "strategies").glob("*.yaml"))
ALL_EXAMPLES = [*TEMPLATES, EXAMPLES / "strategy-example.yaml"]


def test_the_template_folder_is_not_empty():
    assert len(TEMPLATES) >= 4


@pytest.mark.parametrize("path", ALL_EXAMPLES, ids=lambda p: p.name)
def test_example_loads_and_passes_authoring_checks(path):
    assert is_valid_strategy_id(path.stem)
    doc = load_strategy(path)
    authored = parse_strategy_text(path.stem, path.read_text(), authoring=True)
    assert authored == doc
    assert doc.id == path.stem
    assert doc.rules, "a template with no rules teaches nothing"


@pytest.mark.parametrize("path", ALL_EXAMPLES, ids=lambda p: p.name)
def test_example_is_safe_to_copy_in(path):
    """A copied template must not start watching or trading by itself."""
    doc = load_strategy(path)
    assert doc.status == StrategyStatus.DRAFT
    assert doc.authorization != Authorization.AUTO
    assert all(r.state == RuleState.ARMED for r in doc.rules)


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_is_marked_as_an_example_and_indexed(path):
    text = path.read_text()
    assert text.startswith("# TEMPLATE, not advice.")
    assert load_strategy(path).position.ticker == "XYZ"
    assert f"({path.name})" in (EXAMPLES / "strategies" / "README.md").read_text()
