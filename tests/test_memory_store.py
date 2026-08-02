import pytest

from tradewind.memory.store import LAYER_BUDGETS, MemoryError, MemoryStore  # noqa: F401
from tradewind.store.db import connect


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(tmp_path / "memory", connect(tmp_path / "db.sqlite"))


def test_paths(store, tmp_path):
    root = tmp_path / "memory"
    assert store.path_for("profile", None) == root / "user_profile.md"
    assert store.path_for("stock", "aapl") == root / "stocks" / "AAPL.md"
    assert store.path_for("strategy", "aapl-long") == root / "strategies" / "aapl-long.md"
    assert store.path_for("lesson", "earnings-chasing") == root / "lessons" / "earnings-chasing.md"


@pytest.mark.parametrize("layer,key", [
    ("stock", "../etc"), ("stock", "/tmp/x"), ("bogus", "AAPL"),
    ("stock", None), ("strategy", "a b"),
])
def test_invalid_layer_or_key_rejected(store, layer, key):
    with pytest.raises(MemoryError):
        store.path_for(layer, key)


def test_add_and_entries_roundtrip(store):
    out = store.apply("stock", "AAPL", "add", text="Earnings day moves average ±8%")
    assert "AAPL" in out
    out = store.apply("stock", "AAPL", "add", text="Strong services growth thesis")
    assert store.entries("stock", "AAPL") == [
        "- Earnings day moves average ±8%",
        "- Strong services growth thesis",
    ]


def test_replace_and_remove_by_unique_substring(store):
    store.apply("profile", None, "add", text="Risk tolerance: moderate")
    store.apply("profile", None, "add", text="Prefers tech sector")
    store.apply("profile", None, "replace", match="Risk tolerance",
                text="Risk tolerance: conservative")
    assert any("conservative" in e for e in store.entries("profile"))
    store.apply("profile", None, "remove", match="tech sector")
    assert len(store.entries("profile")) == 1


def test_ambiguous_match_errors(store):
    store.apply("profile", None, "add", text="alpha one")
    store.apply("profile", None, "add", text="alpha two")
    with pytest.raises(MemoryError) as ei:
        store.apply("profile", None, "remove", match="alpha")
    assert "2" in str(ei.value)


def test_missing_match_errors(store):
    with pytest.raises(MemoryError):
        store.apply("profile", None, "remove", match="nothing here")


def test_budget_blocks_add_when_full(store):
    big = "x" * 480
    for i in range(5):
        store.apply("profile", None, "add", text=f"{i} {big}")
    with pytest.raises(MemoryError) as ei:
        store.apply("profile", None, "add", text="one more")
    assert "budget" in str(ei.value)


def test_render_for_context_truncates(store):
    for i in range(4):
        store.apply("stock", "NVDA", "add", text=f"note {i} " + "y" * 400)
    out = store.render_for_context("stock", "NVDA", budget=500)
    assert len(out) <= 500 + 60
    assert "truncated" in out
    assert store.read("stock", "NVDA").count("note") == 4  # file intact


def test_memory_log_records_diffs(store, tmp_path):
    store.apply("profile", None, "add", text="hello")
    conn = store._conn
    [row] = conn.execute("SELECT * FROM memory_log").fetchall()
    assert row["layer"] == "profile" and row["action"] == "add"
    assert "hello" in row["after"]
