"""Unit tests for SQLite cache, Custom Timestamp Versioning, and Multi-Folder Bi-Directional Sync Engine."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.db import CachedFileRecord, SyncDatabase, SyncPair, compute_sha256
from src.drive_api import FOLDER_MIME_TYPE, RemoteChangeEvent
from src.sync_engine import SyncEngine
from src.versioning import VersioningEngine


class FakeDriveClient:
    """In-memory fake of DriveClient for deterministic multi-pair bi-directional sync testing."""

    def __init__(self) -> None:
        self.root_id = "root_bearsync_id"
        self.token_counter = 1
        self.files: Dict[str, RemoteChangeEvent] = {}
        self.contents: Dict[str, bytes] = {}
        self.trashed_ids: List[str] = []
        self.pending_changes: List[RemoteChangeEvent] = []

    def ensure_root_folder(self, folder_name: str = "BearSync") -> str:
        return self.ensure_remote_path(folder_name)

    def ensure_remote_path(self, remote_path: str) -> str:
        cleaned = remote_path.strip().replace("\\", "/").strip("/")
        if not cleaned:
            return "root"
        parent = "root"
        for seg in [s.strip() for s in cleaned.split("/") if s.strip()]:
            parent = self.ensure_folder(seg, parent)
        return parent

    def ensure_folder(self, name: str, parent_id: str = "root") -> str:
        folder_id = f"dir_{parent_id}_{name}"
        self.files[folder_id] = RemoteChangeEvent(
            file_id=folder_id,
            removed=False,
            name=name,
            mime_type=FOLDER_MIME_TYPE,
            parents=(parent_id,),
            modified_time=1000.0,
        )
        return folder_id

    def upload_file(
        self,
        local_path: Path,
        parent_id: str,
        sha256: str,
        existing_drive_id: Optional[str] = None,
        remote_name: Optional[str] = None,
    ) -> Tuple[str, float]:
        drive_id = existing_drive_id or f"file_{len(self.files) + 1}_{local_path.name}"
        mtime = local_path.stat().st_mtime
        data = local_path.read_bytes()
        self.contents[drive_id] = data
        self.files[drive_id] = RemoteChangeEvent(
            file_id=drive_id,
            removed=False,
            name=remote_name or local_path.name,
            mime_type="text/plain",
            parents=(parent_id,),
            modified_time=mtime,
            sha256=sha256,
        )
        return drive_id, mtime

    def download_file(
        self, drive_id: str, dest_path: Path, remote_mtime: Optional[float] = None
    ) -> None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(self.contents[drive_id])

    def trash_file(self, drive_id: str) -> None:
        self.trashed_ids.append(drive_id)
        self.files.pop(drive_id, None)

    def get_start_page_token(self) -> str:
        return f"token_{self.token_counter}"

    def fetch_changes(self, page_token: str) -> Tuple[List[RemoteChangeEvent], str]:
        events = list(self.pending_changes)
        self.pending_changes.clear()
        self.token_counter += 1
        return events, f"token_{self.token_counter}"

    def list_remote_tree(self, root_folder_id: str) -> Dict[str, RemoteChangeEvent]:
        result: Dict[str, RemoteChangeEvent] = {}
        for event in self.files.values():
            if not event.removed and root_folder_id in event.parents:
                result[event.name] = event
        return result


def test_sqlite_cache_crud(tmp_path: Path) -> None:
    db = SyncDatabase(tmp_path / "sync_cache.db")
    rec = CachedFileRecord(
        rel_path="Documents/Vault/note.md",
        drive_id="drv_123",
        sha256="abc123sha",
        mtime=1700000000.5,
        is_dir=False,
        remote_mtime=1700000000.5,
    )
    db.upsert_file(rec)

    assert db.get_by_path("Documents/Vault/note.md") == rec
    assert db.get_by_drive_id("drv_123") == rec

    db.set_start_page_token("page_tok_42")
    assert db.get_start_page_token() == "page_tok_42"

    db.delete_by_path("Documents/Vault")
    assert db.get_by_path("Documents/Vault/note.md") is None
    db.close()


def test_custom_timestamp_versioning_and_pruning(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    vault_dir = sync_root / "Documents" / "Vault"
    vault_dir.mkdir(parents=True)

    target_file = vault_dir / "secrets.txt"
    target_file.write_text("v1 content", encoding="utf-8")

    engine = VersioningEngine(
        sync_root=sync_root,
        monitored_paths=["Documents/Vault"],
        enabled=True,
        mode="versions_subdir",
        max_versions_per_file=2,
    )

    assert engine.is_monitored("Documents/Vault/secrets.txt") is True
    assert engine.is_monitored("Pictures/vacation.jpg") is False

    ts1 = datetime(2026, 10, 19, 12, 0, 1)
    ts2 = datetime(2026, 10, 19, 12, 0, 2)
    ts3 = datetime(2026, 10, 19, 12, 0, 3)

    arch1 = engine.archive_before_overwrite("Documents/Vault/secrets.txt", timestamp=ts1)
    assert arch1 is not None and arch1.exists()
    assert arch1.name == "secrets_20261019_120001.txt"

    target_file.write_text("v2 content", encoding="utf-8")
    arch2 = engine.archive_before_overwrite("Documents/Vault/secrets.txt", timestamp=ts2)
    assert arch2 is not None and arch2.exists()

    target_file.write_text("v3 content", encoding="utf-8")
    arch3 = engine.archive_before_overwrite("Documents/Vault/secrets.txt", timestamp=ts3)
    assert arch3 is not None and arch3.exists()

    versions_dir = sync_root / ".versions" / "Documents" / "Vault"
    remaining = sorted(p.name for p in versions_dir.iterdir())
    assert len(remaining) == 2


def test_bidirectional_sync_and_conflict_resolution(tmp_path: Path) -> None:
    sync_root = tmp_path / "GoogleDrive"
    sync_root.mkdir(parents=True)
    db = SyncDatabase(tmp_path / "sync_cache.db")
    fake_drive = FakeDriveClient()
    versioning = VersioningEngine(
        sync_root=sync_root,
        monitored_paths=["*"],
        enabled=True,
        mode="versions_subdir",
    )

    engine = SyncEngine(
        db=db,
        drive=fake_drive,  # type: ignore[arg-type]
        versioning=versioning,
        sync_root=sync_root,
    )

    doc_path = sync_root / "report.txt"
    doc_path.write_text("baseline v1", encoding="utf-8")

    summary1 = engine.run_sync_pass()
    assert summary1.uploaded == 1
    cached = db.get_by_path("report.txt")
    assert cached is not None

    # Simultaneous local & remote modifications -> Conflict resolution
    root_id = str(db.get_settings().get("remote_root_id"))
    doc_path.write_text("local edit v2", encoding="utf-8")
    remote_bytes = b"remote edit v2"
    fake_drive.contents[cached.drive_id] = remote_bytes
    remote_event = RemoteChangeEvent(
        file_id=cached.drive_id,
        removed=False,
        name="report.txt",
        mime_type="text/plain",
        parents=(root_id,),
        modified_time=doc_path.stat().st_mtime + 10.0,
        sha256="remote_sha256_different",
    )
    fake_drive.pending_changes.append(remote_event)

    summary2 = engine.run_sync_pass()
    assert summary2.conflicts_resolved == 1
    assert summary2.downloaded == 1
    assert summary2.versions_archived >= 1

    assert doc_path.read_bytes() == remote_bytes
    conflict_files = list(sync_root.glob("report.conflict.*.txt"))
    assert len(conflict_files) == 1
    assert conflict_files[0].read_text(encoding="utf-8") == "local edit v2"
    assert compute_sha256(doc_path) == db.get_by_path("report.txt").sha256  # type: ignore[union-attr]
    db.close()


def test_multiple_sync_pairs_and_nested_remote_subfolders(tmp_path: Path) -> None:
    db = SyncDatabase(tmp_path / "sync_cache.db")
    fake_drive = FakeDriveClient()

    # Pair 1: Project Data <-> /Backups/Project Data/
    proj_local = tmp_path / "appdata" / "Project Data"
    proj_local.mkdir(parents=True)
    (proj_local / "settings.ini").write_text("[Config]\nTheme=Dark\n", encoding="utf-8")

    # Pair 2: Notes <-> /Documents/Notes
    notes_local = tmp_path / "Notes"
    notes_local.mkdir(parents=True)
    (notes_local / "todo.md").write_text("# Tasks\n", encoding="utf-8")

    pair1 = SyncPair.create(
        local_path=str(proj_local),
        remote_path="/Backups/Project Data/",
    )
    pair2 = SyncPair.create(
        local_path=str(notes_local),
        remote_path="/Documents/Notes",
    )
    db.save_sync_pairs([pair1, pair2])

    default_ver = VersioningEngine(sync_root=tmp_path)
    engine = SyncEngine(
        db=db,
        drive=fake_drive,  # type: ignore[arg-type]
        versioning=default_ver,
        sync_root=tmp_path,
    )

    summary = engine.run_sync_pass()
    assert summary.uploaded == 2

    # Verify nested remote folders were created for /Backups/Project Data
    saved_pairs = db.get_sync_pairs()
    assert saved_pairs[0].remote_path == "/Backups/Project Data"
    assert saved_pairs[0].remote_folder_id == "dir_dir_root_Backups_Project Data"
    assert db.get_by_path("settings.ini", pair1.pair_id) is not None
    assert db.get_by_path("todo.md", pair2.pair_id) is not None
    db.close()
