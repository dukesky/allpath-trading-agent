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

`migrate_files(settings)` is the one-shot, idempotent move from the old shape
to the new one for the `paper` account (the only account that can have
pre-existing data -- `shadow` starts empty by spec). It is called exactly
once, from `app.build_components`, before any store is constructed, so every
store always sees the new layout regardless of how it got there. Also called
by `cli.py`'s `main` before dispatching broker-less commands.

Safety -- what this module does and does not destroy:

* It MOVES files (legacy root -> per-account subdirectory) and REMOVES the
  emptied legacy directories. That is destructive in the ordinary sense: the
  live tree does not look the same afterwards. What it never does is DISCARD
  user content. Every legacy file ends up somewhere under the new layout.
* If -- and only if -- a legacy layout is detected, the ENTIRE memory/ or
  strategies/ tree is copied to a timestamped sibling backup directory
  FIRST, before anything is moved.
* Name collisions (a legacy file whose name is already taken in the new
  per-account directory) keep the EXISTING new-layout file under its own
  name and park the legacy twin beside it as `{name}.legacy`. The legacy
  content stays in the live tree, not only in the backup. The `.legacy`
  suffix is deliberately appended AFTER the extension so the parked file
  matches none of this codebase's content globs (`*.md` in
  web/routes/memory.py, agent/review.py, cli.py; `*.yaml` in
  strategy/store.py) and is therefore never loaded as live data.
* A relative symlink moves one level deeper, so its target text is
  rewritten to keep resolving. If it cannot be rewritten, the entry is left
  where it is (and named on stdout) rather than moved broken -- which also
  means its parent legacy directory survives and the NEXT start will detect
  the legacy shape again and take another backup. That is the intended
  trade: a re-run beats a dead link.
* The backup is deleted in exactly one situation: the backup copy itself
  failed, before any file was moved, so the live tree is still the complete
  original. A failure AFTER the moves start always keeps the backup -- it
  is the only remaining copy of the original layout -- and says so.

A fresh install or an already-migrated install is a strict, backup-free
no-op: detection is based purely on the current on-disk shape (legacy
artifacts still sitting at the root), so a second run over an
already-migrated tree finds nothing to do and never touches the filesystem.
"""

from __future__ import annotations

import os
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


def _backup_path(parent: Path, name: str, timestamp: str) -> Path:
    """Generate a backup directory path, with numeric suffix if collision."""
    base = parent / f"{name}.bak-{timestamp}"
    if not base.exists():
        return base
    # Same-second collision: append numeric suffix (_0, _1, ...)
    i = 0
    while True:
        candidate = parent / f"{name}.bak-{timestamp}_{i}"
        if not candidate.exists():
            return candidate
        i += 1


def _take_backup(directory: Path) -> Path:
    """Copy `directory` to a timestamped sibling before anything is moved.

    This is the ONE place a partial backup may be removed on failure:
    nothing has been moved yet, so the live tree is still the complete
    original and a half-written backup is pure litter. Catches OSError
    (disk full, permissions, a vanished file) as well as `shutil.Error` --
    only the latter was caught before, so an OSError left the partial
    backup on disk AND propagated an unwrapped exception out of startup."""
    backup = _backup_path(directory.parent, directory.name, _timestamp())
    try:
        shutil.copytree(directory, backup, symlinks=True)
    except (shutil.Error, OSError) as exc:
        shutil.rmtree(backup, ignore_errors=True)
        print(f"[migrate] FAILED for {directory.name}/: {exc} — fix the "
              "listed files and restart; nothing was moved")
        raise RuntimeError(f"Migration failed for {directory.name}") from exc
    return backup


def _partial_migration_error(directory: Path, backup: Path,
                             exc: Exception) -> RuntimeError:
    """Post-move failure: the backup is now the ONLY complete copy of the
    original layout, so it is KEPT (this handler used to `rmtree` it --
    deleting the user's only escape hatch at the exact moment they needed
    it), and both the printed line and the raised error point at it."""
    print(f"[migrate] FAILED for {directory.name}/: {exc} — {directory.name}/"
          f" is partially migrated; restore from {backup}")
    return RuntimeError(
        f"Migration failed for {directory.name}: partially migrated; "
        f"restore from {backup}")


def _exists(path: Path) -> bool:
    """`Path.exists()` follows symlinks, so a DANGLING link reads as absent
    -- and moving another file onto its name would silently clobber it."""
    return path.exists() or path.is_symlink()


def _legacy_twin(dst: Path) -> Path:
    """Where a colliding LEGACY file goes. The existing new-layout file at
    `dst` keeps its own name (it is what the running app has been reading
    and writing); the legacy twin is parked beside it rather than discarded
    from the live tree. `.legacy` is appended after the extension so the
    parked file matches none of this codebase's `*.md`/`*.yaml` content
    globs -- see this module's docstring."""
    candidate = dst.with_name(dst.name + ".legacy")
    i = 0
    while _exists(candidate):
        candidate = dst.with_name(f"{dst.name}.legacy{i}")
        i += 1
    return candidate


def _move_symlink(src: Path, dst: Path) -> bool:
    """Move symlink `src` to `dst`, rewriting a RELATIVE target so it still
    resolves from the new (one level deeper) location. Returns False if the
    entry was deliberately left in place instead."""
    target = os.readlink(src)
    if os.path.isabs(target):
        shutil.move(str(src), str(dst))  # absolute targets survive any move
        return True
    try:
        absolute = os.path.normpath(os.path.join(str(src.parent), target))
        rewritten = os.path.relpath(absolute, str(dst.parent))
        dst.symlink_to(rewritten)
    except (OSError, ValueError) as exc:
        print(f"[migrate] skipped {src} — relative symlink to {target!r} "
              f"cannot be rewritten for {dst} ({exc}); left in place")
        return False
    src.unlink()
    return True


def _move_or_merge(src: Path, dst: Path) -> None:
    """Move `src` to `dst`, entry by entry. `dst`'s parent is created if
    needed.

    Walked recursively rather than bulk-`shutil.move`d even when `dst` does
    not exist yet, because two cases need per-entry handling: a relative
    symlink anywhere in the tree needs its target rewritten for the new
    depth (`_move_symlink`), and a name collision under `dst` must park the
    legacy twin rather than drop it (`_legacy_twin`) -- the merge path used
    to `rmtree(src)` here, destroying every colliding legacy file in the
    live tree."""
    dst.parent.mkdir(parents=True, exist_ok=True)

    # Checked BEFORE is_dir(), which follows symlinks: a symlink to a
    # directory must move as a link, not have its contents walked.
    if src.is_symlink():
        _move_symlink(src, _legacy_twin(dst) if _exists(dst) else dst)
        return

    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        for item in sorted(src.iterdir()):
            _move_or_merge(item, dst / item.name)
        try:
            src.rmdir()
        except OSError:
            # Not empty: something was deliberately left behind (an
            # unrewritable symlink). Keep it -- never rmtree the remainder.
            pass
        return

    shutil.move(str(src), str(_legacy_twin(dst) if _exists(dst) else dst))


def _migrate_memory(memory_dir: Path) -> None:
    if not memory_dir.exists():
        return  # fresh install -- MemoryStore creates paths lazily on write
    legacy = [d for d in _LEGACY_MEMORY_SUBDIRS if (memory_dir / d).is_dir()]
    if not legacy:
        return  # already migrated (or this layer was never written)

    backup = _take_backup(memory_dir)

    try:
        target_root = memory_dir / DEFAULT_ACCOUNT
        for name in legacy:
            _move_or_merge(memory_dir / name, target_root / name)
    except Exception as exc:
        raise _partial_migration_error(memory_dir, backup, exc) from exc

    print(f"[migrate] moved legacy memory layers into {memory_dir.name}/{DEFAULT_ACCOUNT}/ (backup: {backup.name})")


def _migrate_strategies(strategies_dir: Path) -> None:
    if not strategies_dir.exists():
        return  # fresh install
    legacy_files = list(strategies_dir.glob("*.yaml"))
    if not legacy_files:
        return  # already migrated (or nothing was ever authored)

    backup = _take_backup(strategies_dir)

    try:
        target = strategies_dir / DEFAULT_ACCOUNT
        target.mkdir(parents=True, exist_ok=True)
        for path in legacy_files:
            # Strategies collision: the existing per-account file keeps its
            # name, the legacy twin is parked beside it as `{name}.legacy`
            # (same keep-existing rule as memory, via `_move_or_merge`).
            # Leaving the colliding legacy file at the root instead -- what
            # this did before -- made detection fire again on the next
            # start, so EVERY build_components() (i.e. every settings save)
            # took another full backup, forever.
            _move_or_merge(path, target / path.name)
        leftover = sorted(p.name for p in strategies_dir.glob("*.yaml"))
        if leftover:
            # Post-condition, so the "no legacy yaml at the root" property
            # detection depends on can never silently regress.
            raise RuntimeError(
                f"legacy strategy files still at the {strategies_dir.name}/"
                f" root after migration: {', '.join(leftover)}")
    except Exception as exc:
        raise _partial_migration_error(strategies_dir, backup, exc) from exc

    print(f"[migrate] moved legacy strategies into {strategies_dir.name}/{DEFAULT_ACCOUNT}/ (backup: {backup.name})")


def migrate_files(settings: Settings) -> None:
    """Idempotent. Safe to call on every process start. Migrates
    `settings.memory_dir` and `settings.strategies_dir` independently --
    either, both, or neither may be legacy on a given install (e.g. a
    strategies-only user with no memory notes yet)."""
    _migrate_memory(settings.memory_dir)
    _migrate_strategies(settings.strategies_dir)
