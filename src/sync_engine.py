"""Bi-directional differential sync engine supporting multiple Local <-> Google Drive SyncPairs."""

from __future__ import annotations

import logging
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Set

from PyQt6.QtCore import QObject, QThread, QTimer, pyqtSignal

from src.db import CachedFileRecord, SyncDatabase, SyncPair, compute_sha256
from src.drive_api import FOLDER_MIME_TYPE, DriveClient, RemoteChangeEvent
from src.versioning import VersioningEngine
from src.watcher import should_ignore_path

logger = logging.getLogger(__name__)


class SyncState(str, Enum):
    """High-level status states for the system tray and UI."""

    IDLE = "idle"
    SYNCING = "syncing"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class SyncSummary:
    """Statistics produced by a single differential or full sync pass."""

    uploaded: int = 0
    downloaded: int = 0
    deleted_local: int = 0
    deleted_remote: int = 0
    conflicts_resolved: int = 0
    versions_archived: int = 0
    errors: int = 0


ProgressCallback = Callable[[str, str, int, int], None]
ActivityCallback = Callable[[str], None]


class SyncEngine:
    """Core bi-directional differential synchronization engine.

    Supports both:
    - Multiple Nextcloud-style `SyncPair` folder connections (e.g. Local Folder <-> `/Backups/Project`)
    - Single default folder mode for backward compatibility
    """

    def __init__(
        self,
        db: SyncDatabase,
        drive: DriveClient,
        versioning: VersioningEngine,
        sync_root: Path,
        suppress_watcher_cb: Optional[Callable[..., None]] = None,
    ) -> None:
        self.db = db
        self.drive = drive
        self.versioning = versioning
        self.sync_root = Path(sync_root).resolve()
        self.suppress_watcher_cb = suppress_watcher_cb
        self._lock = threading.Lock()
        self.conflict_strategy: str = "keep_both"
        self.max_file_size_mb: int = 0

    def _suppress(self, rel_path: str, active: bool, pair_id: str = "default") -> None:
        if self.suppress_watcher_cb is not None:
            try:
                self.suppress_watcher_cb(rel_path, active, pair_id)
            except TypeError:
                self.suppress_watcher_cb(rel_path, active)

    def ensure_remote_root_id(self) -> str:
        """Return or initialize the default remote Google Drive root folder ID."""
        settings = self.db.get_settings()
        root_id = str(settings.get("remote_root_id") or "").strip()
        if root_id:
            return root_id

        folder_name = str(settings.get("remote_folder_name") or "BearSync").strip()
        root_id = self.drive.ensure_root_folder(folder_name)
        self.db.save_settings({"remote_root_id": root_id})
        return root_id

    def get_effective_sync_pairs(self) -> List[SyncPair]:
        """Return enabled SyncPairs, or construct a default SyncPair if none are configured."""
        configured = self.db.get_sync_pairs()
        if configured:
            return [p for p in configured if p.enabled]

        settings = self.db.get_settings()
        remote_name = str(settings.get("remote_folder_name") or "BearSync").strip()
        return [
            SyncPair(
                pair_id="default",
                local_path=str(self.sync_root),
                remote_path=f"/{remote_name.strip('/')}",
                remote_folder_id=str(settings.get("remote_root_id") or ""),
                enabled=True,
                versioning_enabled=self.versioning.enabled,
                versioning_mode=self.versioning.mode,
                versioning_paths=list(self.versioning.monitored_paths),
                max_versions_per_file=self.versioning.max_versions_per_file,
            )
        ]

    def run_sync_pass(
        self,
        force_full: bool = False,
        specific_local_paths: Optional[Iterable[str]] = None,
        target_pair_id: Optional[str] = None,
        progress_cb: Optional[ProgressCallback] = None,
        activity_cb: Optional[ActivityCallback] = None,
    ) -> SyncSummary:
        """Execute a thread-safe bi-directional synchronization cycle across all (or one) SyncPairs."""
        with self._lock:
            settings = self.db.get_settings()
            self.conflict_strategy = str(settings.get("conflict_strategy", "keep_both"))
            self.max_file_size_mb = int(settings.get("max_file_size_mb", 0))

            total_summary = SyncSummary()
            pairs = self.get_effective_sync_pairs()

            for pair in pairs:
                if target_pair_id and pair.pair_id != target_pair_id:
                    continue
                self._sync_single_pair(
                    pair=pair,
                    force_full=force_full,
                    specific_local_paths=specific_local_paths,
                    summary=total_summary,
                    progress_cb=progress_cb,
                    activity_cb=activity_cb,
                )

            self.db.set_state("last_sync_epoch", str(datetime.now().timestamp()))
            return total_summary

    def _sync_single_pair(
        self,
        pair: SyncPair,
        force_full: bool,
        specific_local_paths: Optional[Iterable[str]],
        summary: SyncSummary,
        progress_cb: Optional[ProgressCallback] = None,
        activity_cb: Optional[ActivityCallback] = None,
    ) -> None:
        pair_id = pair.pair_id
        local_root = Path(pair.local_path).expanduser().resolve()
        local_root.mkdir(parents=True, exist_ok=True)

        def emit_progress(detail: str, current: int, total: int) -> None:
            if progress_cb is not None:
                progress_cb(pair_id, detail, current, total)

        def emit_activity(msg: str) -> None:
            if activity_cb is not None:
                ts = datetime.now().strftime("%H:%M:%S")
                activity_cb(f"[{ts}] {msg}")

        emit_progress(f"Scanning {pair.remote_path}...", 0, 0)

        pair_versioning = (
            self.versioning
            if pair_id == "default"
            else VersioningEngine(
                sync_root=local_root,
                monitored_paths=pair.versioning_paths or ["*"],
                enabled=pair.versioning_enabled,
                mode=pair.versioning_mode,
                max_versions_per_file=pair.max_versions_per_file,
            )
        )

        # Resolve or create the remote folder/subfolder path (e.g. /Backups/Project)
        root_id = pair.remote_folder_id.strip()
        if not root_id:
            if pair_id == "default":
                root_id = self.ensure_remote_root_id()
            else:
                emit_progress(f"Creating remote folder {pair.remote_path}...", 0, 0)
                root_id = self.drive.ensure_remote_path(pair.remote_path)
                pair.remote_folder_id = root_id
                self.db.upsert_sync_pair(pair)
                emit_activity(f"Verified Google Drive folder {pair.remote_path}")

        start_token = self.db.get_start_page_token(pair_id)
        if force_full or not start_token:
            emit_progress(f"Listing remote files in {pair.remote_path}...", 0, 0)
            remote_map = self.drive.list_remote_tree(root_id)
            new_token = self.drive.get_start_page_token()
            self._reconcile_full(
                pair_id=pair_id,
                remote_label=pair.remote_path,
                local_root=local_root,
                versioning=pair_versioning,
                root_id=root_id,
                remote_map=remote_map,
                summary=summary,
                progress_cb=emit_progress,
                activity_cb=emit_activity,
            )
            self.db.set_start_page_token(new_token, pair_id)
        else:
            emit_progress(f"Checking Google Drive changes for {pair.remote_path}...", 0, 0)
            events, new_token = self.drive.fetch_changes(start_token)
            if events:
                self._apply_remote_changes(
                    pair_id=pair_id,
                    remote_label=pair.remote_path,
                    local_root=local_root,
                    versioning=pair_versioning,
                    root_id=root_id,
                    events=events,
                    summary=summary,
                    progress_cb=emit_progress,
                    activity_cb=emit_activity,
                )
            self.db.set_start_page_token(new_token, pair_id)

            if specific_local_paths is not None:
                self._sync_local_paths(
                    pair_id=pair_id,
                    remote_label=pair.remote_path,
                    local_root=local_root,
                    versioning=pair_versioning,
                    root_id=root_id,
                    rel_paths=specific_local_paths,
                    summary=summary,
                    progress_cb=emit_progress,
                    activity_cb=emit_activity,
                )
            else:
                self._scan_and_push_local_delta(
                    pair_id=pair_id,
                    remote_label=pair.remote_path,
                    local_root=local_root,
                    versioning=pair_versioning,
                    root_id=root_id,
                    summary=summary,
                    progress_cb=emit_progress,
                    activity_cb=emit_activity,
                )

        emit_progress("Up to date ✓", 1, 1)

    # --- Full Bi-Directional Reconciliation ---

    def _reconcile_full(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        root_id: str,
        remote_map: Dict[str, RemoteChangeEvent],
        summary: SyncSummary,
        progress_cb: Callable[[str, int, int], None],
        activity_cb: Callable[[str], None],
    ) -> None:
        cached_map = self.db.get_all_files(pair_id)
        local_files = self._scan_local_files(local_root)

        all_paths: Set[str] = (
            set(cached_map.keys()) | set(remote_map.keys()) | set(local_files.keys())
        )
        sorted_paths = sorted(all_paths, key=lambda p: (p.count("/"), p))
        total_items = len(sorted_paths)

        for idx, rel_path in enumerate(sorted_paths, start=1):
            if versioning.is_version_or_conflict_artifact(rel_path):
                if Path(rel_path).parts and Path(rel_path).parts[0] == ".versions":
                    continue

            progress_cb(
                f"Syncing {remote_label}: {rel_path} ({idx}/{total_items})",
                idx,
                total_items,
            )

            cached = cached_map.get(rel_path)
            remote = remote_map.get(rel_path)
            local_abs = local_files.get(rel_path)

            if remote and remote.mime_type == FOLDER_MIME_TYPE:
                local_dir = local_root / rel_path
                local_dir.mkdir(parents=True, exist_ok=True)
                self.db.upsert_file(
                    CachedFileRecord(
                        pair_id=pair_id,
                        rel_path=rel_path,
                        drive_id=remote.file_id,
                        sha256="DIR",
                        mtime=local_dir.stat().st_mtime,
                        is_dir=True,
                        remote_mtime=remote.modified_time,
                    )
                )
                continue

            if local_abs is not None and local_abs.is_dir():
                continue

            self._reconcile_single_path(
                pair_id=pair_id,
                remote_label=remote_label,
                local_root=local_root,
                versioning=versioning,
                root_id=root_id,
                rel_path=rel_path,
                local_abs=local_abs,
                cached=cached,
                remote=remote,
                summary=summary,
                activity_cb=activity_cb,
            )

    def _reconcile_single_path(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        root_id: str,
        rel_path: str,
        local_abs: Optional[Path],
        cached: Optional[CachedFileRecord],
        remote: Optional[RemoteChangeEvent],
        summary: SyncSummary,
        activity_cb: Callable[[str], None],
    ) -> None:
        local_exists = local_abs is not None and local_abs.is_file()
        remote_exists = remote is not None and not remote.removed

        local_sha = compute_sha256(local_abs) if local_exists and local_abs else ""
        local_mtime = local_abs.stat().st_mtime if local_exists and local_abs else 0.0
        remote_sha = remote.sha256 if remote_exists and remote else ""

        # Case 1: Both exist
        if local_exists and remote_exists and local_abs and remote:
            if remote_sha and local_sha == remote_sha:
                self.db.upsert_file(
                    CachedFileRecord(
                        pair_id=pair_id,
                        rel_path=rel_path,
                        drive_id=remote.file_id,
                        sha256=local_sha,
                        mtime=local_mtime,
                        is_dir=False,
                        remote_mtime=remote.modified_time,
                    )
                )
                return

            local_changed = cached is None or (local_sha != cached.sha256)
            remote_changed = (
                cached is None
                or (bool(remote_sha) and remote_sha != cached.sha256)
                or (not remote_sha and abs(remote.modified_time - cached.remote_mtime) > 1.0)
            )

            if local_changed and not remote_changed:
                if cached and cached.sha256 != local_sha:
                    if versioning.archive_before_overwrite(rel_path):
                        summary.versions_archived += 1
                self._upload_local_file(
                    pair_id,
                    remote_label,
                    local_root,
                    root_id,
                    rel_path,
                    local_abs,
                    local_sha,
                    remote.file_id,
                    summary,
                    activity_cb,
                )
            elif remote_changed and not local_changed:
                self._download_remote_file(
                    pair_id,
                    remote_label,
                    local_root,
                    versioning,
                    rel_path,
                    remote,
                    summary,
                    activity_cb,
                )
            elif local_changed and remote_changed:
                if self.conflict_strategy == "keep_local":
                    activity_cb(f"⚡ Conflict: preserving local version of {rel_path}")
                    self._upload_local_file(
                        pair_id=pair_id,
                        remote_label=remote_label,
                        local_root=local_root,
                        root_id=root_id,
                        rel_path=rel_path,
                        local_abs=local_abs,
                        local_sha=local_sha,
                        existing_drive_id=remote.file_id,
                        summary=summary,
                        activity_cb=activity_cb,
                    )
                elif self.conflict_strategy == "keep_remote":
                    activity_cb(f"⚡ Conflict: accepting remote version of {rel_path}")
                    self._download_remote_file(
                        pair_id=pair_id,
                        remote_label=remote_label,
                        local_root=local_root,
                        versioning=versioning,
                        rel_path=rel_path,
                        remote=remote,
                        summary=summary,
                        activity_cb=activity_cb,
                    )
                else:
                    self._resolve_conflict(
                        pair_id,
                        remote_label,
                        local_root,
                        versioning,
                        root_id,
                        rel_path,
                        local_abs,
                        remote,
                        summary,
                        activity_cb,
                    )
            return

        # Case 2: Only local exists
        if local_exists and not remote_exists and local_abs:
            if cached is not None and cached.sha256 == local_sha:
                self._delete_local_file(
                    pair_id,
                    remote_label,
                    versioning,
                    rel_path,
                    local_abs,
                    summary,
                    activity_cb,
                )
            else:
                self._upload_local_file(
                    pair_id,
                    remote_label,
                    local_root,
                    root_id,
                    rel_path,
                    local_abs,
                    local_sha,
                    None,
                    summary,
                    activity_cb,
                )
            return

        # Case 3: Only remote exists
        if remote_exists and not local_exists and remote:
            if cached is not None and (
                (remote_sha and remote_sha == cached.sha256)
                or abs(remote.modified_time - cached.remote_mtime) <= 1.0
            ):
                self.drive.trash_file(remote.file_id)
                self.db.delete_by_path(rel_path, pair_id)
                summary.deleted_remote += 1
                activity_cb(f"🗑️ Trashed on Google Drive: {remote_label}/{rel_path}")
            else:
                self._download_remote_file(
                    pair_id,
                    remote_label,
                    local_root,
                    versioning,
                    rel_path,
                    remote,
                    summary,
                    activity_cb,
                )
            return

        # Case 4: Neither exists
        if cached is not None:
            self.db.delete_by_path(rel_path, pair_id)

    # --- Differential Remote Changes (`changes.list`) ---

    def _apply_remote_changes(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        root_id: str,
        events: List[RemoteChangeEvent],
        summary: SyncSummary,
        progress_cb: Callable[[str, int, int], None],
        activity_cb: Callable[[str], None],
    ) -> None:
        folder_id_to_rel: Dict[str, str] = {root_id: ""}
        for rec in self.db.get_all_files(pair_id).values():
            if rec.is_dir:
                folder_id_to_rel[rec.drive_id] = rec.rel_path

        total = len(events)
        for idx, event in enumerate(events, start=1):
            cached = self.db.get_by_drive_id(event.file_id, pair_id)

            if event.removed or event.trashed:
                if cached is not None:
                    local_abs = local_root / cached.rel_path
                    if local_abs.exists() and local_abs.is_file():
                        current_sha = compute_sha256(local_abs)
                        if current_sha == cached.sha256:
                            self._delete_local_file(
                                pair_id,
                                remote_label,
                                versioning,
                                cached.rel_path,
                                local_abs,
                                summary,
                                activity_cb,
                            )
                    else:
                        self.db.delete_by_drive_id(event.file_id, pair_id)
                continue

            rel_path: Optional[str] = None
            for parent_id in event.parents:
                if parent_id in folder_id_to_rel:
                    parent_rel = folder_id_to_rel[parent_id]
                    rel_path = f"{parent_rel}/{event.name}".lstrip("/")
                    break
            if rel_path is None and cached is not None:
                rel_path = cached.rel_path
            if not rel_path:
                continue

            progress_cb(
                f"Applying remote change in {remote_label}: {rel_path} ({idx}/{total})",
                idx,
                total,
            )

            if event.mime_type == FOLDER_MIME_TYPE:
                local_dir = local_root / rel_path
                local_dir.mkdir(parents=True, exist_ok=True)
                folder_id_to_rel[event.file_id] = rel_path
                self.db.upsert_file(
                    CachedFileRecord(
                        pair_id=pair_id,
                        rel_path=rel_path,
                        drive_id=event.file_id,
                        sha256="DIR",
                        mtime=local_dir.stat().st_mtime,
                        is_dir=True,
                        remote_mtime=event.modified_time,
                    )
                )
                continue

            local_abs = local_root / rel_path
            self._reconcile_single_path(
                pair_id=pair_id,
                remote_label=remote_label,
                local_root=local_root,
                versioning=versioning,
                root_id=root_id,
                rel_path=rel_path,
                local_abs=local_abs if local_abs.exists() else None,
                cached=cached or self.db.get_by_path(rel_path, pair_id),
                remote=event,
                summary=summary,
                activity_cb=activity_cb,
            )

    # --- Differential Local Changes (`inotify` & Delta Scan) ---

    def _sync_local_paths(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        root_id: str,
        rel_paths: Iterable[str],
        summary: SyncSummary,
        progress_cb: Callable[[str, int, int], None],
        activity_cb: Callable[[str], None],
    ) -> None:
        path_list = list(rel_paths)
        total = len(path_list)
        for idx, raw_rel in enumerate(path_list, start=1):
            rel_path = raw_rel.strip("/").replace("\\", "/")
            if not rel_path:
                continue
            local_abs = local_root / rel_path
            if should_ignore_path(local_root, local_abs):
                continue

            progress_cb(
                f"Uploading {remote_label}: {rel_path} ({idx}/{total})",
                idx,
                total,
            )

            cached = self.db.get_by_path(rel_path, pair_id)

            if not local_abs.exists():
                if cached is not None:
                    self.drive.trash_file(cached.drive_id)
                    self.db.delete_by_path(rel_path, pair_id)
                    summary.deleted_remote += 1
                    activity_cb(f"🗑️ Trashed on Google Drive: {remote_label}/{rel_path}")
                continue

            if local_abs.is_dir():
                parent_id = self._ensure_remote_parent_dirs(
                    pair_id, local_root, root_id, rel_path
                )
                folder_id = self.drive.ensure_folder(local_abs.name, parent_id)
                self.db.upsert_file(
                    CachedFileRecord(
                        pair_id=pair_id,
                        rel_path=rel_path,
                        drive_id=folder_id,
                        sha256="DIR",
                        mtime=local_abs.stat().st_mtime,
                        is_dir=True,
                    )
                )
                continue

            local_sha = compute_sha256(local_abs)
            if cached is not None and cached.sha256 == local_sha:
                continue

            if cached is not None and cached.sha256 != local_sha:
                if versioning.archive_before_overwrite(rel_path):
                    summary.versions_archived += 1

            self._upload_local_file(
                pair_id=pair_id,
                remote_label=remote_label,
                local_root=local_root,
                root_id=root_id,
                rel_path=rel_path,
                local_abs=local_abs,
                local_sha=local_sha,
                existing_drive_id=cached.drive_id if cached else None,
                summary=summary,
                activity_cb=activity_cb,
            )

    def _scan_and_push_local_delta(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        root_id: str,
        summary: SyncSummary,
        progress_cb: Callable[[str, int, int], None],
        activity_cb: Callable[[str], None],
    ) -> None:
        cached_map = self.db.get_all_files(pair_id)
        local_files = self._scan_local_files(local_root)

        changed_paths: List[str] = []
        for rel_path, local_abs in local_files.items():
            if local_abs.is_dir():
                continue
            cached = cached_map.get(rel_path)
            stat = local_abs.stat()
            if cached is None or abs(stat.st_mtime - cached.mtime) > 1e-4:
                changed_paths.append(rel_path)

        for rel_path, cached in cached_map.items():
            if not cached.is_dir and rel_path not in local_files:
                changed_paths.append(rel_path)

        if changed_paths:
            self._sync_local_paths(
                pair_id,
                remote_label,
                local_root,
                versioning,
                root_id,
                changed_paths,
                summary,
                progress_cb,
                activity_cb,
            )

    # --- Helpers for Upload, Download, Conflict Resolution, and Directory Hierarchy ---

    def _ensure_remote_parent_dirs(
        self, pair_id: str, local_root: Path, root_id: str, rel_path: str
    ) -> str:
        parts = Path(rel_path).parts[:-1]
        current_parent_id = root_id
        accumulated = ""

        for part in parts:
            accumulated = f"{accumulated}/{part}".lstrip("/")
            cached_dir = self.db.get_by_path(accumulated, pair_id)
            if cached_dir and cached_dir.is_dir and cached_dir.drive_id:
                current_parent_id = cached_dir.drive_id
            else:
                folder_id = self.drive.ensure_folder(part, current_parent_id)
                local_dir = local_root / accumulated
                mtime = local_dir.stat().st_mtime if local_dir.exists() else 0.0
                self.db.upsert_file(
                    CachedFileRecord(
                        pair_id=pair_id,
                        rel_path=accumulated,
                        drive_id=folder_id,
                        sha256="DIR",
                        mtime=mtime,
                        is_dir=True,
                    )
                )
                current_parent_id = folder_id

        return current_parent_id

    def _upload_local_file(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        root_id: str,
        rel_path: str,
        local_abs: Path,
        local_sha: str,
        existing_drive_id: Optional[str],
        summary: SyncSummary,
        activity_cb: Callable[[str], None],
    ) -> None:
        if self.max_file_size_mb > 0:
            try:
                size_mb = local_abs.stat().st_size / (1024 * 1024)
                if size_mb > self.max_file_size_mb:
                    activity_cb(
                        f"⏭️ Skipping {rel_path} ({size_mb:.1f} MB exceeds {self.max_file_size_mb} MB limit)"
                    )
                    logger.info("Skipped upload of '%s' (exceeds %d MB limit)", rel_path, self.max_file_size_mb)
                    return
            except OSError:
                pass

        parent_id = self._ensure_remote_parent_dirs(
            pair_id, local_root, root_id, rel_path
        )
        drive_id, remote_mtime = self.drive.upload_file(
            local_path=local_abs,
            parent_id=parent_id,
            sha256=local_sha,
            existing_drive_id=existing_drive_id,
        )
        self.db.upsert_file(
            CachedFileRecord(
                pair_id=pair_id,
                rel_path=rel_path,
                drive_id=drive_id,
                sha256=local_sha,
                mtime=local_abs.stat().st_mtime,
                is_dir=False,
                remote_mtime=remote_mtime,
            )
        )
        summary.uploaded += 1
        activity_cb(f"↑ Uploaded: {rel_path} → {remote_label}")
        logger.info(
            "Uploaded local file [pair=%s] '%s' (drive_id=%s)",
            pair_id,
            rel_path,
            drive_id,
        )

    def _download_remote_file(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        rel_path: str,
        remote: RemoteChangeEvent,
        summary: SyncSummary,
        activity_cb: Callable[[str], None],
    ) -> None:
        if self.max_file_size_mb > 0 and remote.size > 0:
            size_mb = remote.size / (1024 * 1024)
            if size_mb > self.max_file_size_mb:
                activity_cb(
                    f"⏭️ Skipping download of {rel_path} ({size_mb:.1f} MB exceeds {self.max_file_size_mb} MB limit)"
                )
                logger.info("Skipped download of '%s' (exceeds %d MB limit)", rel_path, self.max_file_size_mb)
                return

        dest_abs = local_root / rel_path
        self._suppress(rel_path, True, pair_id)
        try:
            if dest_abs.exists() and dest_abs.is_file():
                if versioning.archive_before_overwrite(rel_path):
                    summary.versions_archived += 1

            self.drive.download_file(
                drive_id=remote.file_id,
                dest_path=dest_abs,
                remote_mtime=remote.modified_time,
            )
            local_sha = compute_sha256(dest_abs)
            self.db.upsert_file(
                CachedFileRecord(
                    pair_id=pair_id,
                    rel_path=rel_path,
                    drive_id=remote.file_id,
                    sha256=local_sha,
                    mtime=dest_abs.stat().st_mtime,
                    is_dir=False,
                    remote_mtime=remote.modified_time,
                )
            )
            summary.downloaded += 1
            activity_cb(f"↓ Downloaded: {remote_label}/{rel_path}")
            logger.info(
                "Downloaded remote file [pair=%s] '%s' (drive_id=%s)",
                pair_id,
                rel_path,
                remote.file_id,
            )
        finally:
            self._suppress(rel_path, False, pair_id)

    def _delete_local_file(
        self,
        pair_id: str,
        remote_label: str,
        versioning: VersioningEngine,
        rel_path: str,
        local_abs: Path,
        summary: SyncSummary,
        activity_cb: Callable[[str], None],
    ) -> None:
        self._suppress(rel_path, True, pair_id)
        try:
            if local_abs.exists() and local_abs.is_file():
                if versioning.archive_before_overwrite(rel_path):
                    summary.versions_archived += 1
                local_abs.unlink()
                summary.deleted_local += 1
                activity_cb(f"🗑️ Deleted locally (removed on Drive): {rel_path}")
            self.db.delete_by_path(rel_path, pair_id)
        finally:
            self._suppress(rel_path, False, pair_id)

    def _resolve_conflict(
        self,
        pair_id: str,
        remote_label: str,
        local_root: Path,
        versioning: VersioningEngine,
        root_id: str,
        rel_path: str,
        local_abs: Path,
        remote: RemoteChangeEvent,
        summary: SyncSummary,
        activity_cb: Callable[[str], None],
    ) -> None:
        """Resolve simultaneous local and remote modification by creating `filename.conflict.[timestamp].[ext]`."""
        conflict_name = versioning.format_conflict_filename(local_abs.name)
        conflict_abs = local_abs.with_name(conflict_name)
        conflict_rel = str(conflict_abs.relative_to(local_root)).replace("\\", "/")

        self._suppress(conflict_rel, True, pair_id)
        try:
            shutil.copy2(local_abs, conflict_abs)
            conflict_sha = compute_sha256(conflict_abs)
            logger.warning(
                "Conflict detected on '%s'! Preserved local changes to '%s'",
                rel_path,
                conflict_rel,
            )
            activity_cb(f"⚠️ Conflict resolved: saved {conflict_rel}")
            self._upload_local_file(
                pair_id=pair_id,
                remote_label=remote_label,
                local_root=local_root,
                root_id=root_id,
                rel_path=conflict_rel,
                local_abs=conflict_abs,
                local_sha=conflict_sha,
                existing_drive_id=None,
                summary=summary,
                activity_cb=activity_cb,
            )
        finally:
            self._suppress(conflict_rel, False, pair_id)

        self._download_remote_file(
            pair_id,
            remote_label,
            local_root,
            versioning,
            rel_path,
            remote,
            summary,
            activity_cb,
        )
        summary.conflicts_resolved += 1

    def _scan_local_files(self, local_root: Optional[Path] = None) -> Dict[str, Path]:
        """Recursively scan `local_root`, excluding ignored temporary files and `.versions/`."""
        target_root = local_root or self.sync_root
        results: Dict[str, Path] = {}
        if not target_root.is_dir():
            return results

        for root, dirs, files in os.walk(target_root):
            root_path = Path(root)
            dirs[:] = [
                d
                for d in dirs
                if not should_ignore_path(target_root, root_path / d)
            ]
            for file_name in files:
                abs_path = root_path / file_name
                if should_ignore_path(target_root, abs_path):
                    continue
                rel_path = str(abs_path.relative_to(target_root)).replace("\\", "/")
                results[rel_path] = abs_path

        return results


class _SyncTaskRunner(QThread):
    """Background QThread executing a SyncEngine pass without blocking the Wayland UI."""

    progress_reported = pyqtSignal(str, str, int, int)  # (pair_id, detail, current, total)
    activity_reported = pyqtSignal(str)
    finished_summary = pyqtSignal(object)
    failed_error = pyqtSignal(str)

    def __init__(
        self,
        engine: SyncEngine,
        force_full: bool = False,
        specific_paths: Optional[List[str]] = None,
        target_pair_id: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self.force_full = force_full
        self.specific_paths = specific_paths
        self.target_pair_id = target_pair_id

    def run(self) -> None:
        try:
            summary = self.engine.run_sync_pass(
                force_full=self.force_full,
                specific_local_paths=self.specific_paths,
                target_pair_id=self.target_pair_id,
                progress_cb=self.progress_reported.emit,
                activity_cb=self.activity_reported.emit,
            )
            self.finished_summary.emit(summary)
        except Exception as exc:
            logger.exception("Sync pass failed: %s", exc)
            self.failed_error.emit(str(exc))


class SyncController(QObject):
    """High-level Qt coordinator managing periodic polling, debounced multi-pair events, and tray state."""

    state_changed = pyqtSignal(str, str)
    progress_updated = pyqtSignal(str, str, int, int)  # (pair_id, detail, current, total)
    activity_logged = pyqtSignal(str)
    sync_completed = pyqtSignal(object)
    conflict_detected = pyqtSignal(int)

    def __init__(
        self,
        engine: SyncEngine,
        poll_interval_seconds: int = 60,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self._paused = False
        self._state = SyncState.IDLE
        self._pending_by_pair: Dict[str, Set[str]] = {}
        self._pending_force_full = False
        self._active_runner: Optional[_SyncTaskRunner] = None

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(max(10, int(poll_interval_seconds)) * 1000)
        self._poll_timer.timeout.connect(self.request_differential_sync)

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def state(self) -> SyncState:
        return self._state

    def start(self) -> None:
        if not self._paused:
            self._poll_timer.start()
            self.request_differential_sync()

    def stop(self) -> None:
        self._poll_timer.stop()
        if self._active_runner is not None and self._active_runner.isRunning():
            self._active_runner.wait(5000)

    def set_poll_interval(self, seconds: int) -> None:
        self._poll_timer.setInterval(max(10, int(seconds)) * 1000)

    def set_paused(self, paused: bool) -> None:
        self._paused = paused
        if paused:
            self._poll_timer.stop()
            self._set_state(SyncState.PAUSED, "Sync Paused")
        else:
            self._set_state(SyncState.IDLE, "Idle • Up to date")
            self._poll_timer.start()
            self.request_differential_sync()

    def request_full_sync(self, pair_id: Optional[str] = None) -> None:
        """Trigger a forced full bi-directional reconciliation ('Sync Now')."""
        if self._paused:
            self.set_paused(False)
        self._pending_force_full = True
        self._launch_if_idle(target_pair_id=pair_id)

    def request_differential_sync(self) -> None:
        if self._paused:
            return
        self._launch_if_idle()

    def enqueue_local_changes(self, rel_paths: List[str]) -> None:
        self.enqueue_pair_local_changes("default", rel_paths)

    def enqueue_pair_local_changes(self, pair_id: str, rel_paths: List[str]) -> None:
        bucket = self._pending_by_pair.setdefault(pair_id, set())
        bucket.update(rel_paths)
        if not self._paused:
            self._launch_if_idle()

    def _launch_if_idle(self, target_pair_id: Optional[str] = None) -> None:
        if self._paused:
            return
        if self._active_runner is not None and self._active_runner.isRunning():
            return

        force_full = self._pending_force_full
        self._pending_force_full = False

        specific_paths: Optional[List[str]] = None
        pair_id = target_pair_id if isinstance(target_pair_id, str) else None
        if not force_full and self._pending_by_pair:
            pair_id, paths_set = next(iter(self._pending_by_pair.items()))
            specific_paths = sorted(paths_set)
            del self._pending_by_pair[pair_id]

        self._set_state(
            SyncState.SYNCING,
            "Syncing all folders..." if force_full else "Syncing changes...",
        )
        runner = _SyncTaskRunner(
            engine=self.engine,
            force_full=force_full,
            specific_paths=specific_paths,
            target_pair_id=pair_id,
            parent=self,
        )
        runner.progress_reported.connect(self._on_runner_progress)
        runner.activity_reported.connect(self.activity_logged.emit)
        runner.finished_summary.connect(self._on_runner_finished)
        runner.failed_error.connect(self._on_runner_failed)
        self._active_runner = runner
        runner.start()

    def _on_runner_progress(
        self, pair_id: str, detail: str, current: int, total: int
    ) -> None:
        self.progress_updated.emit(pair_id, detail, current, total)
        # Avoid spamming the tray over D-Bus with per-file updates; only update on transition
        if self._state != SyncState.SYNCING:
            self._set_state(SyncState.SYNCING, "Syncing...")

    def _on_runner_finished(self, summary: SyncSummary) -> None:
        self._active_runner = None
        if summary.conflicts_resolved > 0:
            self.conflict_detected.emit(summary.conflicts_resolved)
        self.sync_completed.emit(summary)

        if self._paused:
            self._set_state(SyncState.PAUSED, "Sync Paused")
            return

        if self._pending_force_full or self._pending_by_pair:
            self._launch_if_idle()
            return

        timestamp = datetime.now().strftime("%H:%M:%S")
        self._set_state(
            SyncState.IDLE,
            f"Idle • Last synced at {timestamp} (↑{summary.uploaded} ↓{summary.downloaded})",
        )

    def _on_runner_failed(self, error_msg: str) -> None:
        self._active_runner = None
        short_err = error_msg.splitlines()[0][:80] if error_msg else "Unknown sync error"
        self.activity_logged.emit(f"[Error] {short_err}")
        self._set_state(SyncState.ERROR, f"Error: {short_err}")

    def _set_state(self, state: SyncState, message: str) -> None:
        self._state = state
        self.state_changed.emit(state.value, message)
