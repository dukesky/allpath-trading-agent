from pathlib import Path

import pytest

from allpath_trade.config import Settings
from allpath_trade.migrate_files import migrate_files


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, db_path=tmp_path / "t.db",
                    strategies_dir=tmp_path / "strategies",
                    memory_dir=tmp_path / "memory")


def _write_legacy_memory(tmp_path: Path) -> None:
    memory = tmp_path / "memory"
    (memory).mkdir(parents=True, exist_ok=True)
    (memory / "user_profile.md").write_text("- Risk tolerance: moderate\n")
    (memory / "stocks").mkdir()
    (memory / "stocks" / "AAPL.md").write_text("- strong cash flow\n")
    (memory / "strategies").mkdir()
    (memory / "strategies" / "momentum.md").write_text("- buy on breakout\n")
    (memory / "lessons").mkdir()
    (memory / "lessons" / "overtrading.md").write_text("- cut position size\n")


def _write_legacy_strategies(tmp_path: Path) -> None:
    strategies = tmp_path / "strategies"
    strategies.mkdir(parents=True, exist_ok=True)
    (strategies / "a.yaml").write_text("name: A\nstatus: active\n")
    (strategies / "b.yaml").write_text("name: B\nstatus: draft\n")


def test_fresh_install_is_a_strict_noop(tmp_path):
    # Neither directory exists yet -- nothing to do, and nothing should be
    # created (StrategyStore/MemoryStore create what they need lazily).
    settings = _settings(tmp_path)
    migrate_files(settings)
    assert not (tmp_path / "memory").exists()
    assert not (tmp_path / "strategies").exists()
    assert list(tmp_path.iterdir()) == []  # no backups either


def test_fresh_install_with_empty_dirs_is_a_noop(tmp_path):
    # Directories exist (e.g. mkdir'd by a prior partial run / the web
    # settings page) but have no legacy content -- still a no-op.
    (tmp_path / "memory").mkdir()
    (tmp_path / "strategies").mkdir()
    settings = _settings(tmp_path)
    migrate_files(settings)
    assert list((tmp_path / "memory").iterdir()) == []
    assert list((tmp_path / "strategies").iterdir()) == []
    backups = [p for p in tmp_path.iterdir() if ".bak-" in p.name]
    assert backups == []


def test_legacy_layout_migrates_to_paper_with_backup(tmp_path):
    _write_legacy_memory(tmp_path)
    _write_legacy_strategies(tmp_path)
    settings = _settings(tmp_path)

    migrate_files(settings)

    memory = tmp_path / "memory"
    strategies = tmp_path / "strategies"

    # Profile stays at the root, shared, untouched.
    assert (memory / "user_profile.md").read_text() == "- Risk tolerance: moderate\n"

    # Everything else moved under paper/.
    assert not (memory / "stocks").exists()
    assert not (memory / "strategies").exists()
    assert not (memory / "lessons").exists()
    assert (memory / "paper" / "stocks" / "AAPL.md").read_text() == \
        "- strong cash flow\n"
    assert (memory / "paper" / "strategies" / "momentum.md").read_text() == \
        "- buy on breakout\n"
    assert (memory / "paper" / "lessons" / "overtrading.md").read_text() == \
        "- cut position size\n"

    assert not list(strategies.glob("*.yaml"))
    assert (strategies / "paper" / "a.yaml").read_text() == "name: A\nstatus: active\n"
    assert (strategies / "paper" / "b.yaml").read_text() == "name: B\nstatus: draft\n"

    # Backups exist as timestamped siblings, with the full original content.
    memory_backups = list(tmp_path.glob("memory.bak-*"))
    strategies_backups = list(tmp_path.glob("strategies.bak-*"))
    assert len(memory_backups) == 1
    assert len(strategies_backups) == 1
    assert (memory_backups[0] / "stocks" / "AAPL.md").read_text() == \
        "- strong cash flow\n"
    assert (memory_backups[0] / "user_profile.md").exists()
    assert (strategies_backups[0] / "a.yaml").read_text() == "name: A\nstatus: active\n"


def test_second_run_is_a_noop_no_second_backup(tmp_path):
    _write_legacy_memory(tmp_path)
    _write_legacy_strategies(tmp_path)
    settings = _settings(tmp_path)

    migrate_files(settings)
    memory_backups_after_1 = list(tmp_path.glob("memory.bak-*"))
    strategies_backups_after_1 = list(tmp_path.glob("strategies.bak-*"))
    assert len(memory_backups_after_1) == 1
    assert len(strategies_backups_after_1) == 1

    migrate_files(settings)  # second run: legacy layout is gone, must no-op
    memory_backups_after_2 = list(tmp_path.glob("memory.bak-*"))
    strategies_backups_after_2 = list(tmp_path.glob("strategies.bak-*"))
    assert memory_backups_after_2 == memory_backups_after_1
    assert strategies_backups_after_2 == strategies_backups_after_1

    # Data is still exactly where the first run put it -- no double-move,
    # no data loss.
    assert (tmp_path / "memory" / "paper" / "stocks" / "AAPL.md").read_text() == \
        "- strong cash flow\n"
    assert (tmp_path / "strategies" / "paper" / "a.yaml").read_text() == \
        "name: A\nstatus: active\n"


def test_only_strategies_legacy_memory_untouched(tmp_path):
    # Partial legacy: memory was never written (fresh), only strategies
    # predates the account split. Only the strategies tree gets a backup.
    _write_legacy_strategies(tmp_path)
    settings = _settings(tmp_path)

    migrate_files(settings)

    assert not (tmp_path / "memory").exists()
    assert list(tmp_path.glob("memory.bak-*")) == []
    assert (tmp_path / "strategies" / "paper" / "a.yaml").exists()
    assert len(list(tmp_path.glob("strategies.bak-*"))) == 1


def test_only_memory_legacy_strategies_untouched(tmp_path):
    # Partial legacy: strategies was never authored (fresh), only memory
    # predates the account split. Only the memory tree gets a backup.
    _write_legacy_memory(tmp_path)
    settings = _settings(tmp_path)

    migrate_files(settings)

    assert not (tmp_path / "strategies").exists()
    assert list(tmp_path.glob("strategies.bak-*")) == []
    assert (tmp_path / "memory" / "paper" / "stocks" / "AAPL.md").exists()
    assert (tmp_path / "memory" / "user_profile.md").exists()
    assert len(list(tmp_path.glob("memory.bak-*"))) == 1


def test_already_migrated_layout_is_a_noop(tmp_path):
    # Simulate a layout that's already on the new per-account shape (e.g.
    # written directly by a fresh install that already ran build_components
    # once) -- must not be mistaken for legacy and re-migrated/backed-up.
    memory = tmp_path / "memory" / "paper" / "stocks"
    memory.mkdir(parents=True)
    (memory / "AAPL.md").write_text("- already migrated\n")
    (tmp_path / "memory" / "user_profile.md").write_text("- profile\n")
    strategies = tmp_path / "strategies" / "paper"
    strategies.mkdir(parents=True)
    (strategies / "a.yaml").write_text("name: A\n")

    settings = _settings(tmp_path)
    migrate_files(settings)

    assert list(tmp_path.glob("memory.bak-*")) == []
    assert list(tmp_path.glob("strategies.bak-*")) == []
    assert (memory / "AAPL.md").read_text() == "- already migrated\n"
    assert (strategies / "a.yaml").read_text() == "name: A\n"


def test_dangling_symlink_in_legacy_memory_copies_as_symlink(tmp_path, capsys):
    # Dangling symlinks should copy as symlinks (not cause errors).
    # This tests that symlinks=True preserves them intact.
    _write_legacy_memory(tmp_path)
    memory = tmp_path / "memory"
    # Add a dangling symlink -- should copy as-is, not raise
    (memory / "broken_link").symlink_to("/nonexistent/file")

    settings = _settings(tmp_path)
    migrate_files(settings)

    # Migration succeeds and prints output
    captured = capsys.readouterr()
    assert "[migrate] moved legacy memory" in captured.out

    # Backup contains the dangling symlink
    backups = list(tmp_path.glob("memory.bak-*"))
    assert len(backups) == 1
    assert (backups[0] / "broken_link").is_symlink()

    # Legacy tree migrated successfully
    assert (memory / "paper" / "stocks" / "AAPL.md").exists()
    assert (memory / "broken_link").is_symlink()


def test_same_second_backup_collision_uses_numeric_suffix(tmp_path, capsys, monkeypatch):
    # Two migrations in the same second should use numeric suffix (_0, _1, ...)
    # for backup collision avoidance.
    _write_legacy_memory(tmp_path)
    settings = _settings(tmp_path)

    # Manually create the first backup before running migrate_files
    memory = tmp_path / "memory"
    timestamp = "20260101000000"
    backup1 = memory.parent / f"memory.bak-{timestamp}"
    backup1.mkdir()
    (backup1 / "placeholder").write_text("existing backup")

    # Patch _timestamp to return the same value, forcing collision detection
    from allpath_trade import migrate_files as mf
    original_timestamp = mf._timestamp
    mf._timestamp = lambda: timestamp

    try:
        migrate_files(settings)

        # First backup unchanged, second backup created with _0 suffix
        assert (backup1 / "placeholder").read_text() == "existing backup"
        backup2_list = list(tmp_path.glob(f"memory.bak-{timestamp}_*"))
        assert len(backup2_list) == 1
        assert (backup2_list[0] / "stocks" / "AAPL.md").exists()

        # Migration still succeeds
        assert (memory / "paper" / "stocks" / "AAPL.md").exists()
        captured = capsys.readouterr()
        assert "[migrate] moved legacy memory" in captured.out
    finally:
        mf._timestamp = original_timestamp


def test_memory_store_rejects_invalid_account(tmp_path):
    # MemoryStore.__init__ should validate account before using it.
    from allpath_trade.memory.store import MemoryStore
    from allpath_trade.store.db import connect

    with pytest.raises(ValueError, match="invalid account"):
        MemoryStore(tmp_path / "memory", connect(tmp_path / "db.sqlite"),
                    account="../../evil")


def test_strategy_store_rejects_invalid_account(tmp_path):
    # StrategyStore.__init__ should validate account.
    from allpath_trade.store.db import connect
    from allpath_trade.strategy.store import StrategyStore

    with pytest.raises(ValueError, match="invalid account"):
        StrategyStore(tmp_path / "strategies", connect(tmp_path / "db.sqlite"),
                      account="../../evil")


def test_strategy_store_for_account_classmethod(tmp_path):
    # Classmethod should construct the store with account validation
    # and create the directory if needed.
    from allpath_trade.store.db import connect
    from allpath_trade.strategy.store import StrategyStore

    conn = connect(tmp_path / "db.sqlite")
    store = StrategyStore.for_account(tmp_path / "strategies", conn,
                                      account="paper")
    assert store.directory == tmp_path / "strategies" / "paper"
    assert (tmp_path / "strategies" / "paper").exists()
    assert store._account == "paper"


def test_strategy_store_for_account_rejects_invalid(tmp_path):
    # Classmethod should also validate account.
    from allpath_trade.store.db import connect
    from allpath_trade.strategy.store import StrategyStore

    with pytest.raises(ValueError, match="invalid account"):
        StrategyStore.for_account(tmp_path / "strategies",
                                  connect(tmp_path / "db.sqlite"),
                                  account="unknown")


# --- T6 review: failure handling, collisions, symlinks -----------------------

def test_move_failure_keeps_the_backup_and_says_how_to_restore(tmp_path, capsys,
                                                               monkeypatch):
    # C2: the post-move failure handler used to rmtree the backup -- after
    # legacy files had ALREADY been moved out of the live tree. That is the
    # one moment the backup is the only surviving copy of the original
    # layout, so it must never be deleted there.
    _write_legacy_memory(tmp_path)
    settings = _settings(tmp_path)

    from allpath_trade import migrate_files as mf
    real_move = mf.shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst):
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError("disk full")
        return real_move(src, dst)

    monkeypatch.setattr(mf.shutil, "move", flaky_move)

    with pytest.raises(RuntimeError):
        migrate_files(settings)

    backups = list(tmp_path.glob("memory.bak-*"))
    assert len(backups) == 1, "the backup must survive a post-move failure"
    # ...with the complete original layout inside it.
    assert (backups[0] / "stocks" / "AAPL.md").read_text() == "- strong cash flow\n"
    assert (backups[0] / "strategies" / "momentum.md").exists()
    assert (backups[0] / "lessons" / "overtrading.md").exists()
    assert (backups[0] / "user_profile.md").exists()

    out = capsys.readouterr().out
    assert "partially migrated" in out
    assert backups[0].name in out
    assert "nothing was moved" not in out


def test_copytree_failure_removes_the_partial_backup_and_moves_nothing(tmp_path,
                                                                      capsys,
                                                                      monkeypatch):
    # I5: the backup step is the ONE place where deleting a partial backup
    # is safe -- nothing has been moved yet, so the live tree is still the
    # complete original. OSError (not just shutil.Error) must be caught.
    _write_legacy_memory(tmp_path)
    settings = _settings(tmp_path)

    from allpath_trade import migrate_files as mf

    def failing_copytree(src, dst, **kwargs):
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "half-copied.md").write_text("partial\n")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(mf.shutil, "copytree", failing_copytree)

    with pytest.raises(RuntimeError, match="Migration failed"):
        migrate_files(settings)

    assert list(tmp_path.glob("memory.bak-*")) == []  # partial backup cleaned up
    # Live tree completely untouched -- still the legacy layout.
    assert (tmp_path / "memory" / "stocks" / "AAPL.md").read_text() == \
        "- strong cash flow\n"
    assert not (tmp_path / "memory" / "paper").exists()
    assert "nothing was moved" in capsys.readouterr().out


def test_memory_collision_parks_the_legacy_twin_instead_of_deleting_it(tmp_path):
    # C2: the merge path used to `rmtree(src)`, destroying every colliding
    # legacy file in the LIVE tree (only the backup kept a copy). The
    # existing new-layout file still wins its name; the legacy twin is
    # parked beside it.
    _write_legacy_memory(tmp_path)
    already = tmp_path / "memory" / "paper" / "stocks"
    already.mkdir(parents=True)
    (already / "AAPL.md").write_text("- new-layout note\n")

    migrate_files(_settings(tmp_path))

    assert (already / "AAPL.md").read_text() == "- new-layout note\n"
    twin = already / "AAPL.md.legacy"
    assert twin.read_text() == "- strong cash flow\n"
    # Parked, not live: no glob in the codebase (`*.md`) picks it up.
    assert sorted(p.name for p in already.glob("*.md")) == ["AAPL.md"]
    # And the legacy directory is gone, so a second run is a no-op.
    assert not (tmp_path / "memory" / "stocks").exists()


def test_strategies_collision_is_idempotent_across_repeated_runs(tmp_path):
    # I1: a colliding legacy yaml left at the strategies root made EVERY
    # build_components() (i.e. every settings save) take a fresh full
    # backup, forever.
    _write_legacy_strategies(tmp_path)
    paper = tmp_path / "strategies" / "paper"
    paper.mkdir(parents=True)
    (paper / "a.yaml").write_text("name: A-new\nstatus: active\n")
    settings = _settings(tmp_path)

    for _ in range(3):
        migrate_files(settings)

    assert len(list(tmp_path.glob("strategies.bak-*"))) == 1
    # Post-condition: nothing loadable is left at the legacy root.
    assert list((tmp_path / "strategies").glob("*.yaml")) == []
    assert (paper / "a.yaml").read_text() == "name: A-new\nstatus: active\n"
    assert (paper / "a.yaml.legacy").read_text() == "name: A\nstatus: active\n"
    assert (paper / "b.yaml").read_text() == "name: B\nstatus: draft\n"
    # StrategyStore.load_all globs *.yaml -- the parked twin must not be
    # loaded as a second, duplicate strategy.
    assert sorted(p.name for p in paper.glob("*.yaml")) == ["a.yaml", "b.yaml"]


def test_relative_symlink_is_rewritten_to_survive_the_move(tmp_path):
    # I6: memory/{layer}/ moves one level deeper, so a relative symlink
    # inside it stops resolving unless its target text is rewritten.
    _write_legacy_memory(tmp_path)
    memory = tmp_path / "memory"
    shared = memory / "shared_notes"
    shared.mkdir()
    (shared / "note.md").write_text("- shared note\n")
    (memory / "stocks" / "shared").symlink_to("../shared_notes")

    migrate_files(_settings(tmp_path))

    moved = memory / "paper" / "stocks" / "shared"
    assert moved.is_symlink()
    assert moved.resolve() == shared.resolve()
    assert (moved / "note.md").read_text() == "- shared note\n"
    assert not (memory / "stocks").exists()


def test_absolute_symlink_target_is_left_alone(tmp_path):
    _write_legacy_memory(tmp_path)
    memory = tmp_path / "memory"
    target = tmp_path / "outside.md"
    target.write_text("- outside\n")
    (memory / "stocks" / "abs").symlink_to(target)

    migrate_files(_settings(tmp_path))

    moved = memory / "paper" / "stocks" / "abs"
    assert moved.is_symlink()
    assert moved.read_text() == "- outside\n"


def test_unrewritable_symlink_is_left_in_place_and_named(tmp_path, capsys,
                                                         monkeypatch):
    # A relative link whose target can't be re-expressed from the new
    # location is left where it is rather than moved broken -- and the
    # migration says which entry it skipped.
    _write_legacy_memory(tmp_path)
    memory = tmp_path / "memory"
    (memory / "shared_notes").mkdir()
    (memory / "stocks" / "shared").symlink_to("../shared_notes")

    from allpath_trade import migrate_files as mf

    def no_relpath(path, start):
        raise ValueError("path is on mount 'A', start on mount 'B'")

    monkeypatch.setattr(mf.os.path, "relpath", no_relpath)

    migrate_files(_settings(tmp_path))

    out = capsys.readouterr().out
    assert "[migrate] skipped" in out
    assert "shared" in out
    # Left in place, still resolving, never deleted.
    left = memory / "stocks" / "shared"
    assert left.is_symlink()
    assert left.resolve() == (memory / "shared_notes").resolve()
    # Everything else still migrated.
    assert (memory / "paper" / "stocks" / "AAPL.md").exists()
