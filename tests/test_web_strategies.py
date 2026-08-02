import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.strategy.model import RuleState
from allpath_trade.web.app import create_app
from tests.test_sentinel import FakeBroker

STRAT = """
name: "Semis core"
status: active
position: {ticker: AAPL, target_weight: 15%}
rules:
  - {id: r1, type: hard, condition: "price < 100", action: "sell all"}
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    (tmp_path / "strategies" / "semis.yaml").write_text(STRAT)
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def test_list_shows_each_strategy(client):
    assert "Semis core" in client.get("/strategies").text


def test_detail_shows_yaml_and_rules(client):
    body = client.get("/strategies/semis").text
    assert "target_weight" in body
    assert "r1" in body


def test_unknown_strategy_returns_404(client):
    assert client.get("/strategies/nope").status_code == 404


def test_path_traversal_is_refused(client):
    assert client.get("/strategies/..%2f..%2fetc%2fpasswd").status_code in (400, 404)


def test_rearm_resets_a_triggered_rule(client):
    store = client.app.state.holder.get().strategies
    store.set_rule_state("semis", "r1", RuleState.TRIGGERED)
    client.post("/strategies/semis/rules/r1/rearm", follow_redirects=False)
    doc = next(d for d in store.load_all(status=None, errors=[]) if d.id == "semis")
    assert doc.rules[0].state.value == "armed"


def test_rearm_path_traversal_is_refused(client):
    r = client.post("/strategies/..%2f..%2fetc%2fpasswd/rules/r1/rearm",
                    follow_redirects=False)
    assert r.status_code in (400, 404)


def test_detail_of_unparseable_yaml_returns_404_not_500(client):
    (client.app.state.holder.get().strategies.directory / "broken.yaml").write_text(
        "not: [valid, yaml structure for a strategy")
    r = client.get("/strategies/broken")
    assert r.status_code == 404
