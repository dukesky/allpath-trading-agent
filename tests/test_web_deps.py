from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from allpath_trade.web.deps import ComponentHolder


class FakeConn:
    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.closed = False

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeSettings:
    db_path: Path


@dataclass
class FakeComponents:
    settings: FakeSettings
    conn: FakeConn


def _tracking_builder(calls: list):
    """A stand-in for `build_components`: records each call and, when no
    `conn` is passed through, opens a "fresh" one tagged by db_path -- same
    contract `build_components` has (open a new connection when `conn` is
    None, otherwise reuse the one given)."""

    def builder(settings, broker, conn):
        calls.append({"settings": settings, "broker": broker, "conn": conn})
        used_conn = conn if conn is not None else FakeConn(f"conn:{settings.db_path}")
        return FakeComponents(settings=settings, conn=used_conn)

    return builder


def test_rebuild_reuses_the_connection_when_db_path_is_unchanged(tmp_path):
    calls: list = []
    builder = _tracking_builder(calls)
    initial = FakeSettings(db_path=tmp_path / "a.db")
    holder = ComponentHolder(initial, builder=builder, env_file=tmp_path / ".env")
    original_conn = holder.get().conn

    holder.rebuild(FakeSettings(db_path=tmp_path / "a.db"))

    assert holder.get().conn is original_conn
    assert original_conn.closed is False
    # The rebuild call passed the existing connection through rather than
    # asking the builder to open a second one.
    assert calls[-1]["conn"] is original_conn


def test_rebuild_closes_the_old_connection_when_db_path_changes(tmp_path):
    calls: list = []
    builder = _tracking_builder(calls)
    initial = FakeSettings(db_path=tmp_path / "a.db")
    holder = ComponentHolder(initial, builder=builder, env_file=tmp_path / ".env")
    original_conn = holder.get().conn

    holder.rebuild(FakeSettings(db_path=tmp_path / "b.db"))

    new_conn = holder.get().conn
    assert new_conn is not original_conn
    assert original_conn.closed is True
    # The builder was asked to open a fresh connection (no conn handed in).
    assert calls[-1]["conn"] is None
