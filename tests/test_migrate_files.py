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
