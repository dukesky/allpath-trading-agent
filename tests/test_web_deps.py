from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

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
    # options-via-mcp T7: per-account bundles, empty by default so every
    # pre-existing test's fake graph keeps looking like one with no options
    # backend anywhere (rebuild's backend teardown iterates this).
    accounts: dict = field(default_factory=dict)


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


class FakeOptionsBackend:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _bundle_with_backend(backend) -> SimpleNamespace:
    """Shaped like app.AccountComponents as far as rebuild's teardown reads
    it: the backend hangs off `bundle.executor.options_backend` (the
    Sentinel shares the same instance, so rebuild only reaches through the
    executor)."""
    return SimpleNamespace(executor=SimpleNamespace(options_backend=backend))


def test_rebuild_stops_the_old_graphs_options_backend(tmp_path):
    # options-via-mcp T7 review fix: every rebuild swaps in a brand-new
    # Components graph whose paper bundle gets a FRESH McpOptionsBackend --
    # the old graph's backend (possibly a live uvx subprocess + daemon
    # thread) is referenced by nothing afterwards, and its atexit hook only
    # fires at process exit, so rebuild itself must stop it or repeated
    # settings saves leak a subprocess each during a long run.
    backends: list[FakeOptionsBackend] = []

    def builder(settings, broker, conn):
        backend = FakeOptionsBackend()
        backends.append(backend)
        used_conn = conn if conn is not None else FakeConn(f"conn:{settings.db_path}")
        return FakeComponents(
            settings=settings, conn=used_conn,
            accounts={"paper": _bundle_with_backend(backend),
                      "shadow": _bundle_with_backend(None)})

    initial = FakeSettings(db_path=tmp_path / "a.db")
    holder = ComponentHolder(initial, builder=builder, env_file=tmp_path / ".env")

    # Same db_path: the connection is reused, but the graph -- and its
    # options backend -- is still replaced, so the old backend must stop.
    holder.rebuild(FakeSettings(db_path=tmp_path / "a.db"))
    assert backends[0].stopped is True
    assert backends[1].stopped is False  # the live graph's backend keeps running

    # Changed db_path: same teardown alongside the stale-conn close.
    holder.rebuild(FakeSettings(db_path=tmp_path / "b.db"))
    assert backends[1].stopped is True
    assert backends[2].stopped is False


def test_rebuild_tolerates_a_backend_whose_stop_raises(tmp_path):
    # Mirrors the stale-conn close's best-effort spirit: one backend's
    # failing stop() must neither propagate out of rebuild() nor leave the
    # new graph uninstalled.
    class ExplodingBackend:
        def stop(self) -> None:
            raise RuntimeError("boom")

    graphs: list[FakeComponents] = []

    def builder(settings, broker, conn):
        used_conn = conn if conn is not None else FakeConn(f"conn:{settings.db_path}")
        built = FakeComponents(
            settings=settings, conn=used_conn,
            accounts={"paper": _bundle_with_backend(ExplodingBackend())})
        graphs.append(built)
        return built

    initial = FakeSettings(db_path=tmp_path / "a.db")
    holder = ComponentHolder(initial, builder=builder, env_file=tmp_path / ".env")

    holder.rebuild(FakeSettings(db_path=tmp_path / "a.db"))  # must not raise

    assert holder.get() is graphs[-1]


def test_overlapping_rebuilds_never_install_a_closed_connection(tmp_path):
    """Regression test for the race the previous fix wave introduced: two
    `rebuild()` calls overlapping in time must not let the second one
    commit a `Components` whose `.conn` the first one already closed (nor
    abandon a freshly opened connection that nothing ends up closing).

    This drives the exact interleaving from the finding -- "call B
    snapshots `current` before call A commits, then commits *after* A has
    committed and closed" -- using one real background thread for the
    second call, synchronized with `threading.Event`s rather than sleeps
    so nothing here is timing-dependent. A background thread is required
    (rather than a nested same-thread call) because proving the fix's lock
    actually blocks a second caller needs two independent callers
    contending for the same `Lock` object; a same-thread nested call would
    just self-deadlock on a non-reentrant lock instead of demonstrating
    anything.

    Against the pre-fix code (separate, fine-grained lock sections) B is
    never blocked and reliably finishes inside A's window, reproducing the
    corrupted-Components outcome deterministically every run. Against the
    fixed code, B blocks on `_rebuild_lock` until A's commit-and-close has
    completed, so it always rebuilds from the post-A state instead.
    """
    a_path = tmp_path / "a.db"
    new_path = tmp_path / "new.db"
    initial = FakeSettings(db_path=a_path)

    created: list[FakeConn] = []

    def make_conn(settings, conn):
        if conn is not None:
            return conn
        fresh = FakeConn(f"conn:{settings.db_path}")
        created.append(fresh)
        return fresh

    holder = ComponentHolder(
        initial,
        builder=lambda settings, broker, conn: FakeComponents(settings, make_conn(settings, conn)),
        env_file=tmp_path / ".env",
    )
    created.clear()  # don't count the connection opened by __init__

    b_reached_builder = threading.Event()
    a_finished = threading.Event()
    thread_b_box: dict[str, threading.Thread] = {}

    def b_builder(settings, broker, conn):
        # B has already read `current` and decided its branch (that
        # happened in the `rebuild()` frame calling this). Signal that,
        # then wait for A to fully finish (commit + close) before
        # returning -- the very next thing B's `rebuild()` does with this
        # return value is commit it, so this forces B's commit to land
        # strictly after A's close.
        b_reached_builder.set()
        a_finished.wait(timeout=2)
        return FakeComponents(settings, make_conn(settings, conn))

    def a_builder(settings, broker, conn):
        # Swap the builder before starting B's thread so B's call is
        # guaranteed to see `b_builder` (not recurse into `a_builder`).
        holder._builder = b_builder
        thread_b = threading.Thread(
            target=holder.rebuild, args=(FakeSettings(db_path=a_path),)
        )
        thread_b.start()
        thread_b_box["thread"] = thread_b
        # Bounded wait, not a correctness assumption: on the fixed code B
        # is blocked on `_rebuild_lock` (still held by this call) and this
        # reliably times out -- that's the fix working, not a failure. On
        # the pre-fix code B is never blocked and reliably signals well
        # within this window.
        b_reached_builder.wait(timeout=1)
        return FakeComponents(settings, make_conn(settings, conn))

    holder._builder = a_builder
    holder.rebuild(FakeSettings(db_path=new_path))  # this is "A"
    a_finished.set()  # release B (if it was waiting) now that A has committed+closed

    thread_b = thread_b_box["thread"]
    thread_b.join(timeout=2)
    assert not thread_b.is_alive(), "B's rebuild() never completed"

    live = holder.get().conn
    assert live.closed is False, "the live connection must never be a closed one"
    for conn in created:
        assert conn is live or conn.closed, (
            f"connection {conn.tag} is neither the live connection nor closed -- leaked"
        )
