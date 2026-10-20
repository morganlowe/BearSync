"""SQLite database layer (`sync_cache.db`) for multi-folder SyncPairs, file hashes, Drive IDs, and state."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

HASH_CHUNK_SIZE = 1024 * 1024  # 1 MiB streaming chunks for SHA-256 calculation


@dataclass(frozen=True)
class CachedFileRecord:
    """Represents a single tracked file or directory in `sync_cache.db`."""

    rel_path: str
    drive_id: str
    sha256: str
    mtime: float
    is_dir: bool = False
    remote_mtime: float = 0.0
    pair_id: str = "default"


@dataclass
class SyncPair:
    """Represents a Nextcloud-style bi-directional Local <-> Google Drive folder sync connection."""

    pair_id: str
    local_path: str
    remote_path: str  # e.g. "/Backups/Project"
    remote_folder_id: str = ""
    enabled: bool = True
    versioning_enabled: bool = False
    versioning_mode: str = "versions_subdir"  # "versions_subdir" or "inline_suffix"
    versioning_paths: List[str] = field(default_factory=lambda: ["*"])
    max_versions_per_file: int = 10

    @classmethod
    def create(
        cls,
        local_path: str,
        remote_path: str,
        remote_folder_id: str = "",
        enabled: bool = True,
        versioning_enabled: bool = False,
        versioning_mode: str = "versions_subdir",
        versioning_paths: Optional[List[str]] = None,
        max_versions_per_file: int = 10,
    ) -> "SyncPair":
        norm_remote = "/" + remote_path.strip().strip("/").replace("\\", "/")
        if norm_remote == "/":
            norm_remote = "/BearSync"
        return cls(
            pair_id=uuid.uuid4().hex[:12],
            local_path=str(Path(local_path).expanduser()),
            remote_path=norm_remote,
            remote_folder_id=remote_folder_id,
            enabled=enabled,
            versioning_enabled=versioning_enabled,
            versioning_mode=versioning_mode,
            versioning_paths=list(versioning_paths) if versioning_paths else ["*"],
            max_versions_per_file=max_versions_per_file,
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SyncPair":
        norm_remote = "/" + str(data.get("remote_path", "/BearSync")).strip().strip("/").replace("\\", "/")
        if norm_remote == "/":
            norm_remote = "/BearSync"
        return cls(
            pair_id=str(data.get("pair_id") or uuid.uuid4().hex[:12]),
            local_path=str(data.get("local_path") or str(Path.home() / "GoogleDrive")),
            remote_path=norm_remote,
            remote_folder_id=str(data.get("remote_folder_id") or ""),
            enabled=bool(data.get("enabled", True)),
            versioning_enabled=bool(data.get("versioning_enabled", False)),
            versioning_mode=str(data.get("versioning_mode") or "versions_subdir"),
            versioning_paths=list(data.get("versioning_paths") or ["*"]),
            max_versions_per_file=int(data.get("max_versions_per_file", 10)),
        )


def compute_sha256(file_path: Path) -> str:
    """Compute the SHA-256 hex digest of a local file using streaming reads."""
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def get_default_config_dir() -> Path:
    """Return the XDG-compliant configuration directory for BearSync."""
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config) if xdg_config else (Path.home() / ".config")
    config_dir = base / "gdrive-sync"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_default_db_path() -> Path:
    """Return the default path to `sync_cache.db`."""
    return get_default_config_dir() / "sync_cache.db"


class SyncDatabase:
    """Thread-safe SQLite wrapper managing multi-folder sync pairs, file index cache, and state."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = Path(db_path) if db_path else get_default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        """Create or migrate tables and indexes with WAL journaling."""
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")

            # Check if an older single-key `files` table exists without `pair_id`
            cols = [
                row["name"]
                for row in cursor.execute("PRAGMA table_info(files);").fetchall()
            ]
            if cols and "pair_id" not in cols:
                cursor.execute("ALTER TABLE files RENAME TO files_legacy;")
                cursor.execute(
                    """
                    CREATE TABLE files (
                        pair_id TEXT NOT NULL DEFAULT 'default',
                        rel_path TEXT NOT NULL,
                        drive_id TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        is_dir INTEGER NOT NULL DEFAULT 0,
                        remote_mtime REAL NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (pair_id, rel_path)
                    );
                    """
                )
                cursor.execute(
                    """
                    INSERT INTO files (pair_id, rel_path, drive_id, sha256, mtime, is_dir, remote_mtime)
                    SELECT 'default', rel_path, drive_id, sha256, mtime, is_dir, remote_mtime
                    FROM files_legacy;
                    """
                )
                cursor.execute("DROP TABLE files_legacy;")
            else:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS files (
                        pair_id TEXT NOT NULL DEFAULT 'default',
                        rel_path TEXT NOT NULL,
                        drive_id TEXT NOT NULL,
                        sha256 TEXT NOT NULL,
                        mtime REAL NOT NULL,
                        is_dir INTEGER NOT NULL DEFAULT 0,
                        remote_mtime REAL NOT NULL DEFAULT 0.0,
                        PRIMARY KEY (pair_id, rel_path)
                    );
                    """
                )

            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_drive_id ON files(drive_id);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_pair_drive ON files(pair_id, drive_id);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_files_sha256 ON files(sha256);"
            )

            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS sync_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def close(self) -> None:
        """Close the SQLite connection cleanly."""
        with self._lock:
            self._conn.close()

    # --- File Cache Operations (Scoped by `pair_id`) ---

    def upsert_file(self, record: CachedFileRecord) -> None:
        """Insert or update a file record in `sync_cache.db`."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO files (pair_id, rel_path, drive_id, sha256, mtime, is_dir, remote_mtime)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pair_id, rel_path) DO UPDATE SET
                    drive_id = excluded.drive_id,
                    sha256 = excluded.sha256,
                    mtime = excluded.mtime,
                    is_dir = excluded.is_dir,
                    remote_mtime = excluded.remote_mtime;
                """,
                (
                    record.pair_id,
                    record.rel_path,
                    record.drive_id,
                    record.sha256,
                    record.mtime,
                    1 if record.is_dir else 0,
                    record.remote_mtime,
                ),
            )

    def get_by_path(
        self, rel_path: str, pair_id: str = "default"
    ) -> Optional[CachedFileRecord]:
        """Look up a cached file record by its normalized relative path and `pair_id`."""
        with self._lock:
            row = self._conn.execute(
                "SELECT pair_id, rel_path, drive_id, sha256, mtime, is_dir, remote_mtime "
                "FROM files WHERE pair_id = ? AND rel_path = ?;",
                (pair_id, rel_path),
            ).fetchone()
            return self._row_to_record(row) if row else None

    def get_by_drive_id(
        self, drive_id: str, pair_id: Optional[str] = None
    ) -> Optional[CachedFileRecord]:
        """Look up a cached file record by its Google Drive file ID."""
        with self._lock:
            if pair_id is not None:
                row = self._conn.execute(
                    "SELECT pair_id, rel_path, drive_id, sha256, mtime, is_dir, remote_mtime "
                    "FROM files WHERE pair_id = ? AND drive_id = ?;",
                    (pair_id, drive_id),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT pair_id, rel_path, drive_id, sha256, mtime, is_dir, remote_mtime "
                    "FROM files WHERE drive_id = ?;",
                    (drive_id,),
                ).fetchone()
            return self._row_to_record(row) if row else None

    def get_all_files(self, pair_id: str = "default") -> Dict[str, CachedFileRecord]:
        """Return a mapping of `rel_path -> CachedFileRecord` for a given `pair_id`."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT pair_id, rel_path, drive_id, sha256, mtime, is_dir, remote_mtime "
                "FROM files WHERE pair_id = ?;",
                (pair_id,),
            ).fetchall()
            return {row["rel_path"]: self._row_to_record(row) for row in rows}

    def delete_by_path(self, rel_path: str, pair_id: str = "default") -> None:
        """Remove a tracked record by relative path (and any nested children if a folder)."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM files WHERE pair_id = ? AND rel_path = ?;",
                (pair_id, rel_path),
            )
            prefix = f"{rel_path.rstrip('/')}/%"
            self._conn.execute(
                "DELETE FROM files WHERE pair_id = ? AND rel_path LIKE ?;",
                (pair_id, prefix),
            )

    def delete_by_drive_id(
        self, drive_id: str, pair_id: Optional[str] = None
    ) -> None:
        """Remove a tracked record by its Google Drive file ID."""
        with self._lock:
            if pair_id is not None:
                self._conn.execute(
                    "DELETE FROM files WHERE pair_id = ? AND drive_id = ?;",
                    (pair_id, drive_id),
                )
            else:
                self._conn.execute(
                    "DELETE FROM files WHERE drive_id = ?;", (drive_id,)
                )

    def clear_pair_cache(self, pair_id: str) -> None:
        """Delete all cached file metadata for a specific sync pair."""
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE pair_id = ?;", (pair_id,))

    def clear_file_cache(self) -> None:
        """Delete all cached file metadata across all pairs."""
        with self._lock:
            self._conn.execute("DELETE FROM files;")

    # --- Sync Pairs Management ---

    def get_sync_pairs(self) -> List[SyncPair]:
        """Return the list of configured Local <-> Google Drive SyncPairs."""
        settings = self.get_settings()
        raw_pairs = settings.get("sync_pairs")
        if isinstance(raw_pairs, list):
            return [SyncPair.from_dict(item) for item in raw_pairs if isinstance(item, dict)]
        return []

    def save_sync_pairs(self, pairs: List[SyncPair]) -> None:
        """Persist the list of SyncPairs into `settings_json`."""
        self.save_settings({"sync_pairs": [p.to_dict() for p in pairs]})

    def upsert_sync_pair(self, pair: SyncPair) -> None:
        """Add or update a single `SyncPair`."""
        pairs = self.get_sync_pairs()
        replaced = False
        for idx, existing in enumerate(pairs):
            if existing.pair_id == pair.pair_id:
                pairs[idx] = pair
                replaced = True
                break
        if not replaced:
            pairs.append(pair)
        self.save_sync_pairs(pairs)

    def remove_sync_pair(self, pair_id: str) -> None:
        """Remove a `SyncPair` and clear its cached file entries."""
        pairs = [p for p in self.get_sync_pairs() if p.pair_id != pair_id]
        self.save_sync_pairs(pairs)
        self.clear_pair_cache(pair_id)

    # --- Sync State & Preferences Persistence ---

    def get_state(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Retrieve a string value from the `sync_state` key-value store."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM sync_state WHERE key = ?;", (key,)
            ).fetchone()
            return str(row["value"]) if row else default

    def set_state(self, key: str, value: str) -> None:
        """Persist a string value in the `sync_state` key-value store."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO sync_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;
                """,
                (key, value),
            )

    def get_start_page_token(self, pair_id: str = "default") -> Optional[str]:
        """Return the persisted Google Drive `changes.list` startPageToken."""
        key = "start_page_token" if pair_id == "default" else f"start_page_token:{pair_id}"
        return self.get_state(key)

    def set_start_page_token(self, token: str, pair_id: str = "default") -> None:
        """Persist the latest Google Drive `changes.list` startPageToken."""
        key = "start_page_token" if pair_id == "default" else f"start_page_token:{pair_id}"
        self.set_state(key, token)

    def get_settings(self) -> Dict[str, Any]:
        """Load user preferences dictionary with sensible defaults."""
        raw = self.get_state("settings_json")
        defaults: Dict[str, Any] = {
            "sync_folder": str(Path.home() / "GoogleDrive"),
            "remote_folder_name": "BearSync",
            "remote_root_id": "",
            "sync_pairs": [],
            "account_email": "",
            "account_name": "",
            "poll_interval_seconds": 60,
            "debounce_seconds": 3.0,
            "rate_limit_upload_kbps": 0,
            "rate_limit_download_kbps": 0,
            "proxy_mode": "system",  # "system", "none", "manual"
            "proxy_host": "",
            "proxy_port": 8080,
            "proxy_user": "",
            "proxy_pass": "",
            "start_minimized": False,
            "autostart": True,
            "max_file_size_mb": 0,  # 0 = unlimited
            "conflict_strategy": "keep_both",  # "keep_both", "keep_local", "keep_remote"
            "versioning_enabled": False,
            "versioning_mode": "versions_subdir",
            "versioning_paths": ["*"],
            "max_versions_per_file": 10,
            "notifications_enabled": True,
            "setup_completed": False,
        }
        if not raw:
            return defaults
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                defaults.update(loaded)
        except json.JSONDecodeError as exc:
            logger.warning("Corrupt settings_json in sync_cache.db, using defaults: %s", exc)
        return defaults

    def save_settings(self, settings: Dict[str, Any]) -> None:
        """Persist the user preferences dictionary to `sync_cache.db`."""
        current = self.get_settings()
        current.update(settings)
        self.set_state("settings_json", json.dumps(current, indent=2, sort_keys=True))

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> CachedFileRecord:
        return CachedFileRecord(
            pair_id=str(row["pair_id"]),
            rel_path=str(row["rel_path"]),
            drive_id=str(row["drive_id"]),
            sha256=str(row["sha256"]),
            mtime=float(row["mtime"]),
            is_dir=bool(row["is_dir"]),
            remote_mtime=float(row["remote_mtime"]),
        )
