from allpath_trade.store.app_state import AppState
from allpath_trade.store.db import connect


def make_app_state(tmp_path):
    return AppState(connect(tmp_path / "t.db"))


def test_get_missing_key_returns_none(tmp_path):
    app_state = make_app_state(tmp_path)
    assert app_state.get("sentinel_last_pass") is None


def test_set_then_get_round_trips(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set("sentinel_last_pass", "2026-08-09T12:00:00+00:00")
    assert app_state.get("sentinel_last_pass") == "2026-08-09T12:00:00+00:00"


def test_set_twice_upserts_a_single_row_with_the_latest_value(tmp_path):
    conn = connect(tmp_path / "t.db")
    app_state = AppState(conn)

    app_state.set("sentinel_last_pass", "2026-08-09T12:00:00+00:00")
    app_state.set("sentinel_last_pass", "2026-08-09T13:00:00+00:00")

    rows = list(conn.execute("SELECT * FROM app_state WHERE key = 'sentinel_last_pass'"))
    assert len(rows) == 1
    assert rows[0]["value"] == "2026-08-09T13:00:00+00:00"
    assert app_state.get("sentinel_last_pass") == "2026-08-09T13:00:00+00:00"


def test_distinct_keys_do_not_collide(tmp_path):
    app_state = make_app_state(tmp_path)
    app_state.set("a", "1")
    app_state.set("b", "2")
    assert app_state.get("a") == "1"
    assert app_state.get("b") == "2"
