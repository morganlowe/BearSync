"""Timestamp file-rotation and versioning engine for monitored directories."""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

VERSIONS_DIR_NAME = ".versions"
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"
INLINE_VERSION_REGEX = re.compile(r".*_\d{8}_\d{6}(\.[^.]+)?$")
CONFLICT_FILE_REGEX = re.compile(r".*\.conflict\.\d{8}_\d{6}(\.[^.]+)?$")


class VersioningEngine:
    """Manages pre-overwrite timestamped backups for configured relative directory paths.

    Supports two archiving strategies:
    1. `versions_subdir`: Moves/copies the pre-overwrite file into `<sync_root>/.versions/<rel_dir>/filename_YYYYMMDD_HHMMSS.ext`
    2. `inline_suffix`: Copies the pre-overwrite file alongside the original as `filename_YYYYMMDD_HHMMSS.ext`
    """

    def __init__(
        self,
        sync_root: Path,
        monitored_paths: Optional[Iterable[str]] = None,
        enabled: bool = True,
        mode: str = "versions_subdir",
        max_versions_per_file: int = 10,
    ) -> None:
        self.sync_root = Path(sync_root).resolve()
        self.enabled = enabled
        self.mode = mode if mode in ("versions_subdir", "inline_suffix") else "versions_subdir"
        self.max_versions_per_file = max(1, int(max_versions_per_file))
        self.monitored_paths: List[str] = []
        self.set_monitored_paths(monitored_paths or [])

    def set_monitored_paths(self, paths: Iterable[str]) -> None:
        """Normalize and store monitored relative directory prefixes (e.g. 'Documents/Vault')."""
        normalized: List[str] = []
        for raw in paths:
            cleaned = raw.strip().strip("/").replace("\\", "/")
            if cleaned:
                normalized.append(cleaned)
        self.monitored_paths = normalized

    def is_monitored(self, rel_path: str) -> bool:
        """Return True if `rel_path` resides inside any configured monitored directory."""
        if not self.enabled:
            return False
        norm_rel = rel_path.strip("/").replace("\\", "/")
        if not norm_rel or self.is_version_or_conflict_artifact(norm_rel):
            return False

        # If "*" or "." is in monitored_paths, monitor the entire sync root
        if "*" in self.monitored_paths or "." in self.monitored_paths:
            return True

        for prefix in self.monitored_paths:
            if norm_rel == prefix or norm_rel.startswith(f"{prefix}/"):
                return True
        return False

    @staticmethod
    def is_version_or_conflict_artifact(rel_path: str) -> bool:
        """Return True if the relative path is inside `.versions/` or is an inline version/conflict."""
        parts = Path(rel_path).parts
        if VERSIONS_DIR_NAME in parts:
            return True
        filename = Path(rel_path).name
        if INLINE_VERSION_REGEX.match(filename):
            return True
        return False

    @staticmethod
    def format_timestamped_filename(filename: str, timestamp: Optional[datetime] = None) -> str:
        """Insert `_YYYYMMDD_HHMMSS` before the file extension."""
        ts_str = (timestamp or datetime.now()).strftime(TIMESTAMP_FORMAT)
        path_obj = Path(filename)
        stem = path_obj.stem
        suffix = "".join(path_obj.suffixes)
        if suffix and stem.endswith(suffix[: -len(path_obj.suffix)]):
            stem = filename[: -len(suffix)]
        return f"{stem}_{ts_str}{suffix}"

    @staticmethod
    def format_conflict_filename(filename: str, timestamp: Optional[datetime] = None) -> str:
        """Format conflict filename as `filename.conflict.[timestamp].[ext]`."""
        ts_str = (timestamp or datetime.now()).strftime(TIMESTAMP_FORMAT)
        path_obj = Path(filename)
        suffix = path_obj.suffix
        stem = filename[: -len(suffix)] if suffix else filename
        if suffix:
            return f"{stem}.conflict.{ts_str}{suffix}"
        return f"{stem}.conflict.{ts_str}"

    def archive_before_overwrite(
        self, rel_path: str, timestamp: Optional[datetime] = None
    ) -> Optional[Path]:
        """Archive the existing local file at `rel_path` if it falls under a monitored rule.

        Returns the Path to the newly created version archive, or None if not monitored
        or the source file does not exist.
        """
        norm_rel = rel_path.strip("/").replace("\\", "/")
        if not self.is_monitored(norm_rel):
            return None

        source_path = self.sync_root / norm_rel
        if not source_path.exists() or not source_path.is_file():
            return None

        versioned_name = self.format_timestamped_filename(source_path.name, timestamp)
        rel_parent = Path(norm_rel).parent

        if self.mode == "inline_suffix":
            target_dir = self.sync_root / rel_parent
        else:
            target_dir = self.sync_root / VERSIONS_DIR_NAME / rel_parent

        target_dir.mkdir(parents=True, exist_ok=True)
        archive_path = target_dir / versioned_name

        # Handle sub-second collision by appending a counter if needed
        counter = 1
        while archive_path.exists():
            stem = Path(versioned_name).stem
            suffix = Path(versioned_name).suffix
            archive_path = target_dir / f"{stem}_{counter}{suffix}"
            counter += 1

        shutil.copy2(source_path, archive_path)
        logger.info("Archived pre-overwrite version of '%s' -> '%s'", norm_rel, archive_path)

        self._prune_old_versions(target_dir, source_path.name)
        return archive_path

    def _prune_old_versions(self, target_dir: Path, original_filename: str) -> None:
        """Enforce `max_versions_per_file` retention limit in `target_dir`."""
        if self.max_versions_per_file <= 0 or not target_dir.is_dir():
            return

        path_obj = Path(original_filename)
        suffix = "".join(path_obj.suffixes)
        stem = original_filename[: -len(suffix)] if suffix else original_filename
        pattern = re.compile(
            rf"^{re.escape(stem)}_\d{{8}}_\d{{6}}(?:_\d+)?{re.escape(suffix)}$"
        )

        matching = sorted(
            (p for p in target_dir.iterdir() if p.is_file() and pattern.match(p.name)),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in matching[self.max_versions_per_file :]:
            try:
                stale.unlink()
                logger.debug("Pruned old version archive: %s", stale)
            except OSError as exc:
                logger.warning("Failed to prune old version %s: %s", stale, exc)
