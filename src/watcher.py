"""Native Linux `inotify` filesystem monitor via `watchdog` supporting multiple SyncPairs with 3s debounce."""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from PyQt6.QtCore import QObject, pyqtSignal

try:
    from watchdog.events import FileMovedEvent, FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    _HAS_WATCHDOG = True
except ImportError:  # pragma: no cover
    FileMovedEvent = Any  # type: ignore[misc,assignment]
    FileSystemEvent = Any  # type: ignore[misc,assignment]
    FileSystemEventHandler = object  # type: ignore[misc,assignment]
    Observer = None  # type: ignore[misc,assignment]
    _HAS_WATCHDOG = False

from src.db import SyncPair
from src.versioning import VERSIONS_DIR_NAME

logger = logging.getLogger(__name__)

DEFAULT_DEBOUNCE_SECONDS = 3.0
IGNORED_SUFFIXES = (".part", ".tmp", ".swp", ".swo", ".crdownload", ".bak")
IGNORED_PREFIXES = (".~", ".goutputstream-", ".#")
IGNORED_EXACT_NAMES = {"sync_cache.db", "sync_cache.db-wal", "sync_cache.db-shm", ".DS_Store"}
IGNORED_DIRS = {
    VERSIONS_DIR_NAME,
    ".git",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def should_ignore_path(sync_root: Path, abs_path: Path) -> bool:
    """Return True if `abs_path` is a temporary editor file, VCS/venv dir, SQLite journal, or `.versions/` item."""
    try:
        rel = abs_path.resolve().relative_to(sync_root.resolve())
    except ValueError:
        return True

    parts = rel.parts
    if not parts:
        return True

    if any(part in IGNORED_DIRS for part in parts):
        return True

    name = abs_path.name
    if name in IGNORED_EXACT_NAMES:
        return True
    if any(name.startswith(prefix) for prefix in IGNORED_PREFIXES):
        return True
    if any(name.endswith(suffix) for suffix in IGNORED_SUFFIXES):
        return True
    return False


class _DebouncedInotifyHandler(FileSystemEventHandler):
    """Internal `watchdog` handler that coalesces rapid file writes with a 3-second debounce."""

    def __init__(
        self,
        pair_id: str,
        sync_root: Path,
        callback: Callable[[str, List[str]], None],
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
    ) -> None:
        super().__init__()
        self.pair_id = pair_id
        self.sync_root = sync_root.resolve()
        self.callback = callback
        self.debounce_seconds = max(0.1, float(debounce_seconds))
        self._lock = threading.Lock()
        self._timers: Dict[str, threading.Timer] = {}
        self._suppressed_paths: Set[str] = set()

    def suppress_path(self, rel_path: str, suppress: bool = True) -> None:
        """Temporarily suppress inotify events for a path while the sync engine writes to it."""
        norm = rel_path.strip("/").replace("\\", "/")
        with self._lock:
            if suppress:
                self._suppressed_paths.add(norm)
            else:
                self._suppressed_paths.discard(norm)

    def stop_all_timers(self) -> None:
        """Cancel all pending debounce timers on shutdown."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type in ("opened", "closed_no_write", "access"):
            return

        paths_to_schedule: List[Path] = [Path(os.fsdecode(event.src_path))]
        if isinstance(event, FileMovedEvent):
            paths_to_schedule.append(Path(os.fsdecode(event.dest_path)))

        for abs_path in paths_to_schedule:
            if should_ignore_path(self.sync_root, abs_path):
                continue
            try:
                rel_path = str(abs_path.resolve().relative_to(self.sync_root)).replace("\\", "/")
            except ValueError:
                continue

            self._schedule_debounced(rel_path)

    def _schedule_debounced(self, rel_path: str) -> None:
        with self._lock:
            if rel_path in self._suppressed_paths:
                return
            existing = self._timers.pop(rel_path, None)
            if existing is not None:
                existing.cancel()

            timer = threading.Timer(
                self.debounce_seconds, self._fire_path, args=(rel_path,)
            )
            timer.daemon = True
            self._timers[rel_path] = timer
            timer.start()

    def _fire_path(self, rel_path: str) -> None:
        with self._lock:
            self._timers.pop(rel_path, None)
            if rel_path in self._suppressed_paths:
                return
        logger.debug(
            "Debounced inotify event ready for sync [pair=%s]: %s",
            self.pair_id,
            rel_path,
        )
        self.callback(self.pair_id, [rel_path])


class LocalFolderWatcher(QObject):
    """Qt-integrated Linux `inotify` watcher supporting multiple SyncPairs with 3-second write debounce."""

    changes_detected = pyqtSignal(list)
    pair_changes_detected = pyqtSignal(str, list)  # (pair_id, list[rel_path])

    def __init__(
        self,
        sync_root: Optional[Path] = None,
        debounce_seconds: float = DEFAULT_DEBOUNCE_SECONDS,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.sync_root = Path(sync_root).resolve() if sync_root else (Path.home() / "GoogleDrive")
        self.debounce_seconds = debounce_seconds
        self._observer: Optional[Any] = None
        self._handlers: Dict[str, _DebouncedInotifyHandler] = {}
        self._pairs: List[SyncPair] = []

    def set_sync_pairs(
        self,
        pairs: Iterable[SyncPair],
        debounce_seconds: Optional[float] = None,
    ) -> None:
        """Configure the list of multi-folder SyncPairs to monitor via Linux inotify."""
        was_running = self._observer is not None and self._observer.is_alive()
        self.stop()
        self._pairs = list(pairs)
        if debounce_seconds is not None:
            self.debounce_seconds = debounce_seconds
        if was_running:
            self.start()

    def start(self) -> None:
        """Start the `watchdog` inotify observer thread for all active SyncPairs."""
        if not _HAS_WATCHDOG or Observer is None:
            raise RuntimeError(
                "The 'watchdog' library is required for Linux inotify monitoring. "
                "Install python-watchdog via pacman/paru or pip."
            )
        if self._observer is not None and self._observer.is_alive():
            return

        self._observer = Observer()
        self._handlers.clear()

        active_pairs = [p for p in self._pairs if p.enabled]
        if not active_pairs:
            # Fallback to single default sync_root
            self.sync_root.mkdir(parents=True, exist_ok=True)
            handler = _DebouncedInotifyHandler(
                pair_id="default",
                sync_root=self.sync_root,
                callback=self._on_debounced_batch,
                debounce_seconds=self.debounce_seconds,
            )
            self._handlers["default"] = handler
            self._observer.schedule(handler, str(self.sync_root), recursive=True)
        else:
            for pair in active_pairs:
                root_path = Path(pair.local_path).expanduser().resolve()
                root_path.mkdir(parents=True, exist_ok=True)
                handler = _DebouncedInotifyHandler(
                    pair_id=pair.pair_id,
                    sync_root=root_path,
                    callback=self._on_debounced_batch,
                    debounce_seconds=self.debounce_seconds,
                )
                self._handlers[pair.pair_id] = handler
                self._observer.schedule(handler, str(root_path), recursive=True)
                logger.info(
                    "Watching SyncPair '%s': '%s' <-> Drive '%s' (debounce=%.1fs)",
                    pair.pair_id,
                    root_path,
                    pair.remote_path,
                    self.debounce_seconds,
                )

        self._observer.daemon = True
        self._observer.start()

    def stop(self) -> None:
        """Stop the observer thread and cancel pending debounce timers."""
        for handler in self._handlers.values():
            handler.stop_all_timers()
        self._handlers.clear()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=3.0)
            self._observer = None

    def update_sync_root(self, new_root: Path, debounce_seconds: Optional[float] = None) -> None:
        """Restart the watcher on a new default directory or with updated debounce settings."""
        was_running = self._observer is not None and self._observer.is_alive()
        self.stop()
        self.sync_root = Path(new_root).resolve()
        if debounce_seconds is not None:
            self.debounce_seconds = debounce_seconds
        if was_running:
            self.start()

    def suppress_path(
        self, rel_path: str, suppress: bool = True, pair_id: str = "default"
    ) -> None:
        """Temporarily suppress events for `rel_path` during programmatic downloads/conflicts."""
        handler = self._handlers.get(pair_id)
        if handler is not None:
            handler.suppress_path(rel_path, suppress)

    def _on_debounced_batch(self, pair_id: str, rel_paths: List[str]) -> None:
        self.pair_changes_detected.emit(pair_id, rel_paths)
        self.changes_detected.emit(rel_paths)
