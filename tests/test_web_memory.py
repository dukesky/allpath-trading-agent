import pytest
from fastapi.testclient import TestClient

from allpath_trade.config import Settings
from allpath_trade.web.app import create_app
from tests.helpers import assert_english_only
from tests.test_sentinel import FakeBroker


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "strategies").mkdir()
    settings = Settings(_env_file=None, db_path=tmp_path / "t.db",
                        strategies_dir=tmp_path / "strategies",
                        memory_dir=tmp_path / "memory", web_token="secret")
    with TestClient(create_app(settings, broker=FakeBroker())) as c:
        c.post("/login", data={"token": "secret"})
        yield c


def test_layers_are_rendered(client):
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="prefers dividend payers")
    body = client.get("/memory").text
    assert "dividend payers" in body
    assert "Profile" in body


def test_audit_trail_is_shown(client):
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="likes semis")
    assert "add" in client.get("/memory").text


def test_memory_page_is_english_only(client):
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="prefers dividend payers")
    assert_english_only(client.get("/memory").text)


def test_page_has_no_edit_controls(client):
    body = client.get("/memory").text.lower()
    assert "<textarea" not in body
    assert "delete" not in body
    assert "<form" not in body


def test_fresh_install_with_no_memory_directory_renders_ok(client):
    # No apply() has run yet, so `memory_dir` was never created on disk --
    # this is the state of every install right after setup.
    r = client.get("/memory")
    assert r.status_code == 200
    assert "Empty." in r.text


def test_stock_dossier_is_listed_under_its_own_key(client):
    # Regression check: MemoryStore.path_for() stores stock dossiers under
    # the "stocks" subdirectory (plural), not "stock" -- the route must glob
    # the same directory the store actually writes to.
    c = client.app.state.holder.get()
    c.memory.apply("stock", "aapl", "add", text="strong cash flow")
    body = client.get("/memory").text
    assert "AAPL" in body
    assert "strong cash flow" in body


def test_strategy_and_lesson_layers_are_listed_under_their_own_keys(client):
    # Regression check: "strategy" and "lesson" are keyed layers too (one
    # file per key), not flat files like "profile" -- the per-key loop must
    # cover them, not just "stock". If the loop ever regressed to special-
    # casing only "stock" again, path_for("strategy"/"lesson", None) would
    # raise MemoryStoreError and 500 the whole page.
    c = client.app.state.holder.get()
    c.memory.apply("strategy", "momentum", "add", text="buy on breakout")
    c.memory.apply("lesson", "overtrading", "add", text="cut position size")
    body = client.get("/memory").text
    assert "momentum" in body
    assert "buy on breakout" in body
    assert "overtrading" in body
    assert "cut position size" in body


def test_stray_file_with_invalid_key_name_is_skipped(client):
    # A file that never went through apply() -- an editor backup, a sync
    # tool, a user poking around the memory directory -- can have a stem
    # that MemoryStore's key pattern rejects (e.g. a space). apply() itself
    # can never produce such a file, since it enforces the same pattern on
    # every write. The route must skip the bad file, not 500 the page.
    c = client.app.state.holder.get()
    c.memory.apply("stock", "aapl", "add", text="strong cash flow")
    stray = c.memory.root / "stocks" / "stray backup.md"
    stray.write_text("not a valid key")
    r = client.get("/memory")
    assert r.status_code == 200
    assert "AAPL" in r.text
    assert "strong cash flow" in r.text


def test_html_in_a_memory_entry_is_rendered_inert(client):
    # Memory entries can contain text sourced from news/search results the
    # agent has read. The guard blocks URLs and imperative phrasing, but not
    # bare HTML -- the template, not the guard, is what must keep this inert.
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="<img src=x onerror=alert(1)>")
    body = client.get("/memory").text
    # The raw tag must never appear as live markup; escaped, its literal
    # text (including the harmless-as-text "onerror=" substring) is fine.
    assert "<img" not in body
    assert "&lt;img" in body


def test_audit_trail_content_is_also_rendered_inert(client):
    # The `after` column in memory_log carries the same untrusted text as
    # the live layer file, so the audit table needs the same protection.
    c = client.app.state.holder.get()
    c.memory.apply("profile", None, "add", text="<b>bold</b> pick")
    body = client.get("/memory").text
    assert "<b>bold</b>" not in body
