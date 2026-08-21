"""Startup migration: legacy single-account memory/ and strategies/ layouts
move into per-account subdirectories (shadow-dual-active T2, spec §②).

Before shadow-dual-active, every install had:
  memory/user_profile.md, memory/stocks/*.md, memory/strategies/*.md,
  memory/lessons/*.md
  strategies/*.yaml

After this task, `MemoryStore`/`StrategyStore` read/write:
  memory/user_profile.md                     (still shared, unmoved)
  memory/{account}/stocks/*.md, .../strategies/*.md, .../lessons/*.md
  strategies/{account}/*.yaml

`migrate_files` is the one-shot, idempotent move from the old shape to the
new one for the `paper` account (the only account that can have pre-existing
data -- `shadow` starts empty by spec). It is called exactly once, from
`app.build_components`, before any store is constructed, so every store
always sees the new layout regardless of how it got there.

Safety: nothing here is ever destructive. If -- and only if -- a legacy
layout is detected, the ENTIRE memory/ or strategies/ tree is copied to a
timestamped sibling backup directory FIRST, before anything is moved. A
fresh install or an already-migrated install is a strict, backup-free no-op:
detection is based purely on the current on-disk shape (legacy artifacts
still sitting at the root), so a second run over an already-migrated tree
finds nothing to do and never touches the filesystem.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

from allpath_trade.config import Settings
from allpath_trade.store.accounts import DEFAULT_ACCOUNT

# The three per-key memory layers that move under memory/{account}/ --
# "profile" is deliberately absent: it stays shared at memory/user_profile.md
# forever (see memory/store.py's path_for). Mirrors the layer->subdir map
# there; kept as a separate literal (not imported) so this module has no
# import-time dependency on memory internals beyond Settings.
_LEGACY_MEMORY_SUBDIRS = ("stocks", "strategies", "lessons")


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d%H%M%S")


def _move_or_merge(src: Path, dst: Path) -> None:
    """Move `src` to `dst`. `dst`'s parent is created if needed. If `dst`
    already exists as a directory (not expected on the detection-gated path
    below, but cheap to make correct rather than assume), merge file-by-file
    instead of letting `shutil.move` nest `src` *inside* `dst`."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        shutil.move(str(src), str(dst))
        return
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            _move_or_merge(item, target)
        elif not target.exists():
            shutil.move(str(item), str(target))
    shutil.rmtree(src, ignore_errors=True)


def _migrate_memory(memory_dir: Path) -> None:
    if not memory_dir.exists():
        return  # fresh install -- MemoryStore creates paths lazily on write
    legacy = [d for d in _LEGACY_MEMORY_SUBDIRS if (memory_dir / d).is_dir()]
    if not legacy:
        return  # already migrated (or this layer was never written)

    backup = memory_dir.parent / f"{memory_dir.name}.bak-{_timestamp()}"
    shutil.copytree(memory_dir, backup)

    target_root = memory_dir / DEFAULT_ACCOUNT
    for name in legacy:
        _move_or_merge(memory_dir / name, target_root / name)


def _migrate_strategies(strategies_dir: Path) -> None:
    if not strategies_dir.exists():
        return  # fresh install
    legacy_files = list(strategies_dir.glob("*.yaml"))
    if not legacy_files:
        return  # already migrated (or nothing was ever authored)

    backup = strategies_dir.parent / f"{strategies_dir.name}.bak-{_timestamp()}"
    shutil.copytree(strategies_dir, backup)

    target = strategies_dir / DEFAULT_ACCOUNT
    target.mkdir(parents=True, exist_ok=True)
    for path in legacy_files:
        shutil.move(str(path), str(target / path.name))


def migrate_files(settings: Settings) -> None:
    """Idempotent. Safe to call on every process start. Migrates
    `settings.memory_dir` and `settings.strategies_dir` independently --
    either, both, or neither may be legacy on a given install (e.g. a
    strategies-only user with no memory notes yet)."""
    _migrate_memory(settings.memory_dir)
    _migrate_strategies(settings.strategies_dir)
