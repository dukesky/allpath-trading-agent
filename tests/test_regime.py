from pathlib import Path

import pytest

from allpath_trade.config import Settings
from allpath_trade.regime import (
    RegimeError,
    effective_auto_apply,
    get_regime,
    regime_authorization,
    regime_key,
    regime_status,
    set_regime,
)
from allpath_trade.store.app_state import AppState
from allpath_trade.store.db import connect
from allpath_trade.strategy.model import Authorization
from allpath_trade.strategy.store import StrategyStore


def _yaml(name: str, status: str, authorization: str) -> str:
    return f"""
name: "{name}"
status: {status}
authorization: {authorization}
position: {{ticker: AAPL, target_weight: 10%}}
rules:
  - {{id: r1, type: hard, condition: "price < 100", action: "sell all"}}
"""


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, **kw)


@pytest.fixture()
def env(tmp_path: Path):
    files = {
        "a_auto": ("active", "auto"),
        "b_confirm": ("active", "confirm"),
        "c_notify": ("active", "notify"),
        "d_paused": ("paused", "auto"),
        "e_draft": ("draft", "auto"),
        "f_archived": ("archived", "auto"),
    }
    for sid, (status, auth) in files.items():
        (tmp_path / f"{sid}.yaml").write_text(_yaml(sid, status, auth))
    conn = connect(tmp_path / "t.db")
    return conn, AppState(conn), StrategyStore(tmp_path, conn)


def _auth(strategies, sid):
    return strategies.load(sid).authorization


def _set(env, regime="human", by="Tian", account="paper", note="", settings=None):
    conn, app_state, strategies = env
    return set_regime(app_state=app_state, conn=conn, strategies=strategies,
                      settings=settings or _settings(), account=account,
                      regime=regime, by=by, note=note)


def test_key_and_authorization_mapping():
    assert regime_key("paper") == "regime:paper"
    assert regime_authorization("human") == Authorization.CONFIRM
    assert regime_authorization("auto") == Authorization.AUTO


def test_precedence_no_regime_uses_global_flag(env):
    _, app_state, _ = env
    assert get_regime(app_state, "paper") is None
    assert effective_auto_apply(app_state, _settings(), "paper") is False
    on = _settings(experiment_auto_apply_revisions=True)
    assert effective_auto_apply(app_state, on, "paper") is True


def test_precedence_shadow_setting_overrides_global_for_shadow_only(env):
    _, app_state, _ = env
    s = _settings(experiment_auto_apply_revisions=False,
                  shadow_experiment_auto_apply_revisions=True)
    assert effective_auto_apply(app_state, s, "shadow") is True
    assert effective_auto_apply(app_state, s, "paper") is False
    s2 = _settings(experiment_auto_apply_revisions=True,
                   shadow_experiment_auto_apply_revisions=False)
    assert effective_auto_apply(app_state, s2, "shadow") is False


def test_precedence_regime_wins_over_both(env):
    _, app_state, _ = env
    app_state.set(regime_key("paper"), "human")
    on = _settings(experiment_auto_apply_revisions=True,
                   shadow_experiment_auto_apply_revisions=True)
    assert effective_auto_apply(app_state, on, "paper") is False
    app_state.set(regime_key("paper"), "auto")
    assert effective_auto_apply(app_state, _settings(), "paper") is True
    # a regime stored for shadow (e.g. by hand) still wins for shadow
    app_state.set(regime_key("shadow"), "auto")
    assert effective_auto_apply(app_state, _settings(), "shadow") is True


def test_set_human_flips_only_active_auto_to_confirm(env):
    _, app_state, strategies = env
    status = _set(env, "human")
    assert get_regime(app_state, "paper") == "human"
    assert _auth(strategies, "a_auto") == Authorization.CONFIRM
    assert _auth(strategies, "b_confirm") == Authorization.CONFIRM
    assert _auth(strategies, "c_notify") == Authorization.NOTIFY
    for sid in ("d_paused", "e_draft", "f_archived"):
        assert _auth(strategies, sid) == Authorization.AUTO
    assert status.regime == "human"
    assert status.auto_apply is False
    assert status.auto_apply_source == "regime"
    assert status.mismatches == []
    assert status.authorization_counts == {"confirm": 2, "notify": 1}
    assert len(status.events) == 1
    ev = status.events[0]
    assert ev["strategies_flipped"] == 1
    assert (ev["account"], ev["regime"], ev["by"]) == ("paper", "human", "Tian")
    assert ev["ts"].endswith("+00:00")
    rows = strategies.versions("a_auto")
    assert "regime human (set by Tian)" in rows[0]["reason"]


def test_set_auto_flips_confirm_to_auto(env):
    _, _, strategies = env
    status = _set(env, "auto")
    assert _auth(strategies, "b_confirm") == Authorization.AUTO
    assert _auth(strategies, "c_notify") == Authorization.NOTIFY
    assert status.auto_apply is True
    assert status.events[0]["strategies_flipped"] == 1


def test_second_identical_call_flips_zero_and_still_logs(env):
    _set(env, "human")
    status = _set(env, "human", note="again")
    assert len(status.events) == 2
    assert status.events[0]["strategies_flipped"] == 0
    assert status.events[0]["note"] == "again"
    assert status.events[1]["strategies_flipped"] == 1


def test_shadow_is_refused(env):
    _, app_state, _ = env
    with pytest.raises(RegimeError, match="paper-only"):
        _set(env, "auto", account="shadow")
    assert get_regime(app_state, "shadow") is None


def test_empty_by_and_bad_regime_raise(env):
    _, app_state, strategies = env
    with pytest.raises(RegimeError):
        _set(env, "human", by="   ")
    with pytest.raises(RegimeError):
        _set(env, "chaos")
    assert get_regime(app_state, "paper") is None
    assert _auth(strategies, "a_auto") == Authorization.AUTO


def test_status_reports_mismatch(env):
    conn, app_state, strategies = env
    app_state.set(regime_key("paper"), "human")
    status = regime_status(app_state=app_state, conn=conn, strategies=strategies,
                           settings=_settings(), account="paper")
    assert status.mismatches == ["a_auto"]
    assert status.authorization_counts == {"auto": 1, "confirm": 1, "notify": 1}


def test_status_without_regime_shows_fallback_source(env):
    conn, app_state, strategies = env
    s = regime_status(app_state=app_state, conn=conn, strategies=strategies,
                      settings=_settings(), account="paper")
    assert s.regime is None and s.auto_apply_source == "global flag"
    assert s.mismatches == []
    sh = regime_status(settings=_settings(shadow_experiment_auto_apply_revisions=True),
                       app_state=app_state, conn=conn, strategies=strategies,
                       account="shadow")
    assert sh.auto_apply_source == "shadow setting" and sh.auto_apply is True


def test_events_newest_first_capped_at_ten_and_account_filtered(env):
    conn, _, _ = env
    for i in range(12):
        _set(env, "human" if i % 2 == 0 else "auto", note=f"n{i}")
    conn.execute("INSERT INTO regime_events (ts, account, regime, by)"
                 " VALUES ('2099-01-01T00:00:00+00:00', 'shadow', 'auto', 'x')")
    conn.commit()
    status = _set(env, "human", note="last")
    assert len(status.events) == 10
    assert status.events[0]["note"] == "last"
    assert status.events[1]["note"] == "n11"
    assert all(e["account"] == "paper" for e in status.events)
