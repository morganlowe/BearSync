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


def test_conflict_strategies_keep_local_and_remote(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    file_path = sync_root / "document.txt"
    file_path.write_text("Local v1", encoding="utf-8")

    db = SyncDatabase(tmp_path / "cache.db")
    fake_drive = FakeDriveClient()
    versioning = VersioningEngine(sync_root=sync_root, enabled=False)

    engine = SyncEngine(
        db=db,
        drive=fake_drive,  # type: ignore[arg-type]
        versioning=versioning,
        sync_root=sync_root,
    )

    # Initial sync
    engine.run_sync_pass()

    # Create conflict: modify both local and remote
    file_path.write_text("Local modified", encoding="utf-8")
    cached = db.get_by_path("document.txt")
    assert cached is not None
    drv_id = cached.drive_id
    fake_drive.files[drv_id] = RemoteChangeEvent(
        file_id=drv_id,
        removed=False,
        name="document.txt",
        mime_type="text/plain",
        parents=(fake_drive.ensure_root_folder(),),
        modified_time=2000.0,
        sha256="fake_remote_hash",
        size=100,
    )
    fake_drive.contents[drv_id] = b"Remote modified"

    # Strategy: keep_local
    db.save_settings({"conflict_strategy": "keep_local"})
    activities: List[str] = []
    summary = engine.run_sync_pass(force_full=True, activity_cb=activities.append)
    assert summary.uploaded == 1
    assert any("Conflict: preserving local version" in a for a in activities)
    assert fake_drive.contents[drv_id] == b"Local modified"

    # Next test strategy: keep_remote
    file_path.write_text("Local modified again", encoding="utf-8")
    fake_drive.contents[drv_id] = b"Remote wins"
    fake_drive.files[drv_id] = RemoteChangeEvent(
        file_id=drv_id,
        removed=False,
        name="document.txt",
        mime_type="text/plain",
        parents=(fake_drive.ensure_root_folder(),),
        modified_time=3000.0,
        sha256="fake_remote_hash_2",
        size=11,
    )
    db.save_settings({"conflict_strategy": "keep_remote"})
    activities.clear()
    summary = engine.run_sync_pass(force_full=True, activity_cb=activities.append)
    assert summary.downloaded == 1
    assert any("Conflict: accepting remote version" in a for a in activities)
    assert file_path.read_text(encoding="utf-8") == "Remote wins"
    db.close()


def test_max_file_size_limit(tmp_path: Path) -> None:
    sync_root = tmp_path / "sync"
    sync_root.mkdir()
    big_file = sync_root / "large.iso"
    big_file.write_bytes(b"0" * (2 * 1024 * 1024))  # 2 MB

    db = SyncDatabase(tmp_path / "cache.db")
    db.save_settings({"max_file_size_mb": 1})  # 1 MB max

    fake_drive = FakeDriveClient()
    versioning = VersioningEngine(sync_root=sync_root, enabled=False)
    engine = SyncEngine(
        db=db,
        drive=fake_drive,  # type: ignore[arg-type]
        versioning=versioning,
        sync_root=sync_root,
    )

    activities: List[str] = []
    summary = engine.run_sync_pass(activity_cb=activities.append)
    assert summary.uploaded == 0
    assert any("exceeds 1 MB limit" in a for a in activities)
    db.close()


def test_proxy_and_rate_limits() -> None:
    import os
    from src.drive_api import DriveClient

    client = DriveClient()
    client.apply_network_settings(
        proxy_mode="manual",
        proxy_host="127.0.0.1",
        proxy_port=8888,
        proxy_user="user",
        proxy_pass="pass",
        rate_limit_upload_kbps=512,
        rate_limit_download_kbps=1024,
    )
    assert client.rate_limit_upload_kbps == 512
    assert client.rate_limit_download_kbps == 1024
    assert os.environ.get("http_proxy") == "http://user:pass@127.0.0.1:8888"

    client.apply_network_settings(proxy_mode="none")
    assert os.environ.get("no_proxy") == "*"

    client.apply_network_settings(proxy_mode="system")
    assert "no_proxy" not in os.environ


def test_autostart_logic(tmp_path: Path, monkeypatch=None) -> None:
    import src.autostart as auto

    old_file = auto.AUTOSTART_FILE
    old_dir = auto.AUTOSTART_DIR
    try:
        auto.AUTOSTART_DIR = tmp_path / "autostart"
        auto.AUTOSTART_FILE = auto.AUTOSTART_DIR / "bearsync.desktop"

        assert not auto.is_autostart_enabled()
        assert auto.set_autostart_enabled(True)
        assert auto.is_autostart_enabled()
        assert auto.AUTOSTART_FILE.exists()
        content = auto.AUTOSTART_FILE.read_text(encoding="utf-8")
        assert "Type=Application" in content
        assert "BearSync" in content

        assert auto.set_autostart_enabled(False)
        assert not auto.is_autostart_enabled()
        assert not auto.AUTOSTART_FILE.exists()
    finally:
        auto.AUTOSTART_FILE = old_file
        auto.AUTOSTART_DIR = old_dir


def test_oauth_credentials_management(tmp_path: Path) -> None:
    from src.drive_api import DriveClient

    client = DriveClient(config_dir=tmp_path / "gdrive-sync")
    assert not client.has_client_credentials()

    creds_path = client.save_client_credentials("my-test-client-id", "my-test-client-secret")
    assert creds_path.is_file()
    assert client.has_client_credentials()

    info = client.get_client_credentials_info()
    assert info["client_id"] == "my-test-client-id"
    assert info["client_secret"] == "my-test-client-secret"
    assert "..." in info["masked_secret"]



