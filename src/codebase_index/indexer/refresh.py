"""Keep the index current for read commands, with one writer at a time.

An agent that edits a file and then asks `refs` or `impact` about it must not get
the answer from before the edit. Read commands therefore check freshness first and,
when indexable files changed, run the incremental update before answering. The check
is read-only (`compute_freshness`), so a fresh index costs no write.

Every writer (this refresh, `update`, `index`, `watch`) takes the same lock file
next to the database. Parallel agents that all find the index stale queue on the
lock; each re-checks freshness once it holds the lock, so only the first one does
the update and the rest read its result.

Set CBX_NO_AUTO_REFRESH=1 to turn the refresh off; reads then report the index as
stale instead of refreshing it.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Iterator, Optional

from ..config import Config
from ..models import IndexFreshness

AUTO_REFRESH_OFF_ENV = "CBX_NO_AUTO_REFRESH"
LOCK_NAME = "index.lock"
# A read waits this long for another writer before answering from the current index.
REFRESH_LOCK_TIMEOUT_S = 120.0
# An explicit `update` or `index` waits longer: the caller asked for the write.
WRITE_LOCK_TIMEOUT_S = 600.0
_POLL_S = 0.05


def auto_refresh_enabled() -> bool:
    return os.environ.get(AUTO_REFRESH_OFF_ENV, "").strip().lower() not in ("1", "true", "yes")


@dataclass
class RefreshResult:
    # None when no freshness was measured (an index from an older schema).
    freshness: Optional[IndexFreshness]
    refreshed_files: int = 0
    seconds: float = 0.0
    rebuilt: bool = False
    lock_timed_out: bool = False
    notes: list[str] = field(default_factory=list)


@contextlib.contextmanager
def write_lock(cache_dir: Path, timeout: float = WRITE_LOCK_TIMEOUT_S) -> Iterator[bool]:
    """Hold the index writer lock; yields False when `timeout` passes without it."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    handle = open(cache_dir / LOCK_NAME, "a+b")
    held = False
    try:
        deadline = time.monotonic() + timeout
        while not (held := _try_lock(handle)):
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_S)
        yield held
    finally:
        if held:
            _unlock(handle)
        handle.close()


if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def remove_db_files(db_path: Path) -> None:
    """Delete the SQLite db and its WAL/SHM sidecars (used to force a clean rebuild)."""
    for p in (db_path, *(db_path.with_name(db_path.name + s) for s in ("-wal", "-shm"))):
        if p.exists():
            p.unlink()


def run_update(
    cfg: Config,
    db_path: Path,
    *,
    since: Optional[str] = None,
    all_files: bool = False,
    timeout: float = WRITE_LOCK_TIMEOUT_S,
):
    """`update` under the writer lock. Returns (stats, rebuilt), or None on lock timeout.

    An index built under an older schema is rebuilt from scratch: an incremental
    write would target tables the current code no longer matches.
    """
    from ..storage.db import SCHEMA_VERSION, Database, peek_schema_version
    from .pipeline import build_index, update_index

    root = Path(cfg.root)
    with write_lock(db_path.parent, timeout) as held:
        if not held:
            return None
        if peek_schema_version(db_path) < SCHEMA_VERSION:
            remove_db_files(db_path)
            with Database(db_path) as db:
                return build_index(cfg, db, root=root), True
        with Database(db_path) as db:
            return update_index(cfg, db, root=root, since=since, all_files=all_files), False


def refresh_if_stale(
    cfg: Config, db_path: Path, *, timeout: float = REFRESH_LOCK_TIMEOUT_S
) -> RefreshResult:
    """Bring an existing index up to date with the working tree before a read."""
    from ..storage.db import SCHEMA_VERSION, Database, peek_schema_version
    from .freshness import compute_freshness

    root = Path(cfg.root)
    if peek_schema_version(db_path) < SCHEMA_VERSION:
        # An index from an older release keeps answering in place; only an explicit
        # `update` or `index` rebuilds it (a read never turns into a full rebuild).
        return RefreshResult(
            freshness=None,
            notes=["the index predates this version's schema; answering from it as is "
                   "until `codebase-index update` rebuilds it"],
        )
    with Database(db_path) as db:
        freshness = compute_freshness(db.conn, root, cfg)
    # `exists` is false while a first build is still writing: wait for its lock
    # below rather than answer from a half-built index.
    if (freshness.exists and not freshness.stale) or not auto_refresh_enabled():
        return RefreshResult(freshness=freshness)

    start = time.perf_counter()
    with write_lock(db_path.parent, timeout) as held:
        if not held:
            return RefreshResult(freshness=freshness, lock_timed_out=True,
                                 seconds=time.perf_counter() - start)
        from .pipeline import build_index, update_index

        with Database(db_path) as db:
            # Another writer may have refreshed while this one waited for the lock.
            freshness = compute_freshness(db.conn, root, cfg)
            if freshness.exists and not freshness.stale:
                return RefreshResult(freshness=freshness, seconds=time.perf_counter() - start)
        if not freshness.exists:
            # A build that never finished (built_at unset): start over cleanly.
            remove_db_files(db_path)
            with Database(db_path) as db:
                stats = build_index(cfg, db, root=root)
                freshness = compute_freshness(db.conn, root, cfg)
            return RefreshResult(freshness=freshness, refreshed_files=stats.indexed,
                                 seconds=time.perf_counter() - start, rebuilt=True)
        with Database(db_path) as db:
            stats = update_index(cfg, db, root=root)
            from ..storage import repo

            freshness = IndexFreshness(
                exists=True,
                stale=False,
                built_at=repo.get_meta(db.conn, "built_at"),
                head_commit=repo.get_meta(db.conn, "head_commit"),
            )
    return RefreshResult(
        freshness=freshness,
        refreshed_files=stats.indexed + stats.deleted,
        seconds=time.perf_counter() - start,
    )
