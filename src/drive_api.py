"""Google Drive API v3 wrapper with loopback OAuth2, remote folder browser/creator, and differential sync."""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.db import get_default_config_dir

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
GOOGLE_APPS_MIME_PREFIX = "application/vnd.google-apps."
DRIVE_FILE_FIELDS = (
    "id, name, mimeType, parents, trashed, modifiedTime, sha256Checksum, md5Checksum, size, appProperties"
)

# Built-in BearSync Desktop OAuth2 client configuration enabling one-click browser sign-in
DEFAULT_OAUTH_CLIENT_CONFIG: Dict[str, Any] = {
    "installed": {
        "client_id": "your-client-id.apps.googleusercontent.com",
        "client_secret": "your-client-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "redirect_uris": ["http://localhost", "http://127.0.0.1"],
    }
}


@dataclass(frozen=True)
class RemoteFolderItem:
    """Represents a Google Drive folder node for the interactive folder browser."""

    folder_id: str
    name: str
    parent_id: str = "root"


@dataclass(frozen=True)
class RemoteChangeEvent:
    """Represents a single change item from Google Drive `changes.list`."""

    file_id: str
    removed: bool
    name: str = ""
    mime_type: str = ""
    parents: Tuple[str, ...] = ()
    trashed: bool = False
    modified_time: float = 0.0
    sha256: str = ""
    size: int = 0


def parse_rfc3339_timestamp(iso_str: Optional[str]) -> float:
    """Convert an RFC 3339 timestamp string from Google Drive API to POSIX epoch seconds."""
    if not iso_str:
        return 0.0
    try:
        cleaned = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        return dt.astimezone(timezone.utc).timestamp()
    except ValueError:
        return 0.0


class DriveAPIError(RuntimeError):
    """Raised when Google Drive API authentication or operations fail."""


class DriveClient:
    """Wrapper around Google Drive API v3 supporting OAuth2 loopback, folder creation, and differential sync."""

    def __init__(
        self,
        config_dir: Optional[Path] = None,
        credentials_path: Optional[Path] = None,
        token_path: Optional[Path] = None,
        service: Optional[Any] = None,
        rate_limit_upload_kbps: int = 0,
        rate_limit_download_kbps: int = 0,
    ) -> None:
        self.config_dir = Path(config_dir) if config_dir else get_default_config_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_path = (
            Path(credentials_path)
            if credentials_path
            else (self.config_dir / "credentials.json")
        )
        self.token_path = (
            Path(token_path) if token_path else (self.config_dir / "token.json")
        )
        self._service = service
        self.rate_limit_upload_kbps = max(0, int(rate_limit_upload_kbps))
        self.rate_limit_download_kbps = max(0, int(rate_limit_download_kbps))

    def apply_network_settings(
        self,
        proxy_mode: str = "system",
        proxy_host: str = "",
        proxy_port: int = 8080,
        proxy_user: str = "",
        proxy_pass: str = "",
        rate_limit_upload_kbps: int = 0,
        rate_limit_download_kbps: int = 0,
    ) -> None:
        """Apply proxy environment variables and rate limits for Drive API transfers."""
        self.rate_limit_upload_kbps = max(0, int(rate_limit_upload_kbps))
        self.rate_limit_download_kbps = max(0, int(rate_limit_download_kbps))

        if proxy_mode == "none":
            os.environ["no_proxy"] = "*"
            os.environ.pop("http_proxy", None)
            os.environ.pop("https_proxy", None)
            os.environ.pop("all_proxy", None)
            os.environ.pop("HTTP_PROXY", None)
            os.environ.pop("HTTPS_PROXY", None)
            os.environ.pop("ALL_PROXY", None)
        elif proxy_mode == "manual" and proxy_host.strip():
            auth = f"{proxy_user}:{proxy_pass}@" if proxy_user else ""
            url = f"http://{auth}{proxy_host.strip()}:{proxy_port}"
            os.environ["http_proxy"] = url
            os.environ["https_proxy"] = url
            os.environ["all_proxy"] = url
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
        else:
            # "system" - clean up any forced overrides so OS/environment defaults take precedence
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)

    # --- OAuth2 & Account Management ---

    def has_valid_token(self) -> bool:
        """Return True if a cached OAuth2 token exists and is valid or refreshable."""
        if self._service is not None:
            return True
        if not self.token_path.is_file():
            return False
        try:
            from google.oauth2.credentials import Credentials

            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            return bool(creds and (creds.valid or creds.refresh_token))
        except Exception as exc:
            logger.debug("Cached token check failed: %s", exc)
            return False

    def save_client_credentials_json(self, raw_json: str) -> Path:
        """Validate and save user-provided OAuth2 client secret JSON to `credentials.json`."""
        try:
            parsed = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            raise DriveAPIError(f"Invalid OAuth JSON syntax: {exc}") from exc

        if not isinstance(parsed, dict) or (
            "installed" not in parsed and "web" not in parsed
        ):
            raise DriveAPIError(
                "OAuth JSON must contain an 'installed' or 'web' client configuration block."
            )

        self.credentials_path.parent.mkdir(parents=True, exist_ok=True)
        self.credentials_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")
        os.chmod(self.credentials_path, 0o600)
        return self.credentials_path

    def authenticate(self, interactive: bool = True, open_browser: bool = True) -> bool:
        """Authenticate with Google Drive API v3 using cached token or loopback OAuth2."""
        if self._service is not None:
            return True

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise DriveAPIError(
                "Google API client libraries are not installed. "
                "Install google-api-python-client and google-auth-oauthlib."
            ) from exc

        creds: Optional[Credentials] = None
        if self.token_path.is_file():
            try:
                creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except Exception as exc:
                logger.warning("Failed to load cached token.json: %s", exc)

        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self._persist_token(creds)
            except Exception as exc:
                logger.warning("Token refresh failed: %s", exc)
                creds = None

        if not creds or not creds.valid:
            if not interactive:
                raise DriveAPIError(
                    "OAuth2 token is missing or expired and interactive login is disabled."
                )
            if self.credentials_path.is_file():
                flow = InstalledAppFlow.from_client_secrets_file(
                    str(self.credentials_path), SCOPES
                )
            else:
                flow = InstalledAppFlow.from_client_config(
                    DEFAULT_OAUTH_CLIENT_CONFIG, SCOPES
                )
            creds = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                open_browser=open_browser,
                authorization_prompt_message="Please complete authentication in your browser: {url}",
                success_message="BearSync authentication complete! You may close this browser tab.",
            )
            self._persist_token(creds)

        self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return True

    def disconnect_account(self) -> None:
        """Remove cached OAuth2 token to sign out or switch Google accounts."""
        self._service = None
        if self.token_path.exists():
            self.token_path.unlink(missing_ok=True)

    def get_account_info(self) -> Dict[str, Any]:
        """Fetch the authenticated Google user's display name, email, and Drive quota."""
        try:
            about = (
                self.service.about()
                .get(fields="user(displayName,emailAddress),storageQuota(limit,usage)")
                .execute()
            )
            user = about.get("user") or {}
            quota = about.get("storageQuota") or {}
            return {
                "display_name": str(user.get("displayName") or ""),
                "email": str(user.get("emailAddress") or ""),
                "usage_bytes": int(quota.get("usage") or 0),
                "limit_bytes": int(quota.get("limit") or 0),
            }
        except Exception as exc:
            logger.debug("Could not fetch Google Drive account info: %s", exc)
            return {"display_name": "", "email": "", "usage_bytes": 0, "limit_bytes": 0}

    def _persist_token(self, creds: Any) -> None:
        """Save OAuth2 token JSON with strict 0600 user-only file permissions."""
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        os.chmod(self.token_path, 0o600)

    @property
    def service(self) -> Any:
        """Return the authenticated Drive v3 service instance."""
        if self._service is None:
            self.authenticate(interactive=False)
        return self._service

    # --- Remote Folder Browsing & Multi-Level Path Creation ---

    def list_subfolders(self, parent_id: str = "root") -> List[RemoteFolderItem]:
        """List all non-trashed subfolders directly inside `parent_id`, sorted alphabetically."""
        folders: List[RemoteFolderItem] = []
        page_token: Optional[str] = None
        while True:
            query = (
                f"'{parent_id}' in parents and mimeType = '{FOLDER_MIME_TYPE}' "
                "and trashed = false"
            )
            response = (
                self.service.files()
                .list(
                    q=query,
                    spaces="drive",
                    fields="nextPageToken, files(id, name)",
                    orderBy="name_natural",
                    pageSize=200,
                    pageToken=page_token,
                )
                .execute()
            )
            for item in response.get("files", []):
                folders.append(
                    RemoteFolderItem(
                        folder_id=str(item["id"]),
                        name=str(item["name"]),
                        parent_id=parent_id,
                    )
                )
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return folders

    def ensure_root_folder(self, folder_name: str = "BearSync") -> str:
        """Find or create a top-level folder or multi-segment path in the user's Google Drive."""
        return self.ensure_remote_path(folder_name)

    def ensure_remote_path(self, remote_path: str) -> str:
        """Resolve or create an arbitrary multi-level Google Drive folder path (e.g. `/Backups/Project`).

        Walks each path segment starting from Google Drive `root`, creating any missing folders/subfolders
        along the way, and returns the final leaf `folder_id`.
        """
        cleaned = remote_path.strip().replace("\\", "/").strip("/")
        if not cleaned:
            return "root"

        segments = [seg.strip() for seg in cleaned.split("/") if seg.strip()]
        current_parent = "root"
        for segment in segments:
            current_parent = self.ensure_folder(segment, current_parent)
        return current_parent

    def ensure_folder(self, name: str, parent_id: str = "root") -> str:
        """Find or create a child folder named `name` under `parent_id`."""
        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = (
            f"name = '{escaped_name}' and mimeType = '{FOLDER_MIME_TYPE}' "
            f"and '{parent_id}' in parents and trashed = false"
        )
        response = (
            self.service.files()
            .list(q=query, spaces="drive", fields="files(id, name)", pageSize=5)
            .execute()
        )
        files = response.get("files", [])
        if files:
            return str(files[0]["id"])

        metadata = {
            "name": name,
            "mimeType": FOLDER_MIME_TYPE,
            "parents": [parent_id],
        }
        created = self.service.files().create(body=metadata, fields="id").execute()
        folder_id = str(created["id"])
        logger.info(
            "Created Google Drive folder '%s' under parent=%s (id=%s)",
            name,
            parent_id,
            folder_id,
        )
        return folder_id

    # --- File Upload, Download & Trash ---

    def upload_file(
        self,
        local_path: Path,
        parent_id: str,
        sha256: str,
        existing_drive_id: Optional[str] = None,
        remote_name: Optional[str] = None,
    ) -> Tuple[str, float]:
        """Upload or update a file on Google Drive, storing its SHA-256 in `appProperties`."""
        from googleapiclient.http import MediaFileUpload

        name = remote_name or local_path.name
        mime_type, _ = mimetypes.guess_type(name)
        if not mime_type:
            mime_type = "application/octet-stream"

        mtime = local_path.stat().st_mtime
        iso_mtime = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        body: Dict[str, Any] = {
            "name": name,
            "modifiedTime": iso_mtime,
            "appProperties": {
                "bearsync_sha256": sha256,
                "bearsync_mtime": f"{mtime:.6f}",
            },
        }
        chunk_size = 256 * 1024 if self.rate_limit_upload_kbps > 0 else -1
        media = MediaFileUpload(str(local_path), mimetype=mime_type, resumable=True, chunksize=chunk_size)

        if self.rate_limit_upload_kbps > 0:
            if existing_drive_id:
                request = self.service.files().update(
                    fileId=existing_drive_id,
                    body=body,
                    media_body=media,
                    fields=DRIVE_FILE_FIELDS,
                )
            else:
                body["parents"] = [parent_id]
                request = self.service.files().create(
                    body=body,
                    media_body=media,
                    fields=DRIVE_FILE_FIELDS,
                )
            result = None
            while result is None:
                t0 = time.time()
                status, result = request.next_chunk()
                expected = (256 * 1024) / (self.rate_limit_upload_kbps * 1024)
                elapsed = time.time() - t0
                if expected > elapsed:
                    time.sleep(expected - elapsed)
        else:
            if existing_drive_id:
                result = (
                    self.service.files()
                    .update(
                        fileId=existing_drive_id,
                        body=body,
                        media_body=media,
                        fields=DRIVE_FILE_FIELDS,
                    )
                    .execute()
                )
            else:
                body["parents"] = [parent_id]
                result = (
                    self.service.files()
                    .create(
                        body=body,
                        media_body=media,
                        fields=DRIVE_FILE_FIELDS,
                    )
                    .execute()
                )

        drive_id = str(result["id"])
        remote_mtime = parse_rfc3339_timestamp(result.get("modifiedTime")) or mtime
        return drive_id, remote_mtime

    def download_file(self, drive_id: str, dest_path: Path, remote_mtime: Optional[float] = None) -> None:
        """Download a file from Google Drive atomically via a temporary sibling file."""
        from googleapiclient.http import MediaIoBaseDownload

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        request = self.service.files().get_media(fileId=drive_id)

        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{dest_path.name}.", suffix=".part", dir=str(dest_path.parent)
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                chunksize = 256 * 1024 if self.rate_limit_download_kbps > 0 else (4 * 1024 * 1024)
                downloader = MediaIoBaseDownload(handle, request, chunksize=chunksize)
                done = False
                while not done:
                    t0 = time.time()
                    _, done = downloader.next_chunk()
                    if self.rate_limit_download_kbps > 0 and not done:
                        expected = chunksize / (self.rate_limit_download_kbps * 1024)
                        elapsed = time.time() - t0
                        if expected > elapsed:
                            time.sleep(expected - elapsed)

            if remote_mtime and remote_mtime > 0:
                os.utime(tmp_path, (remote_mtime, remote_mtime))
            tmp_path.replace(dest_path)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    def trash_file(self, drive_id: str) -> None:
        """Move a remote file or folder to the Google Drive trash."""
        try:
            self.service.files().update(fileId=drive_id, body={"trashed": True}).execute()
        except Exception as exc:
            logger.warning("Failed to trash remote file %s: %s", drive_id, exc)

    # --- Differential Remote Polling (`changes.list`) & Tree Listing ---

    def get_start_page_token(self) -> str:
        """Fetch the initial `startPageToken` for `changes.list` polling."""
        response = self.service.changes().getStartPageToken().execute()
        return str(response["startPageToken"])

    def fetch_changes(self, page_token: str) -> Tuple[List[RemoteChangeEvent], str]:
        """Poll Google Drive `changes.list` from `page_token`."""
        events: List[RemoteChangeEvent] = []
        current_token: Optional[str] = page_token
        new_start_token: str = page_token

        while current_token:
            response = (
                self.service.changes()
                .list(
                    pageToken=current_token,
                    spaces="drive",
                    includeItemsFromAllDrives=False,
                    supportsAllDrives=False,
                    fields=(
                        f"nextPageToken, newStartPageToken, "
                        f"changes(fileId, removed, file({DRIVE_FILE_FIELDS}))"
                    ),
                    pageSize=200,
                )
                .execute()
            )
            for item in response.get("changes", []):
                file_id = str(item.get("fileId", ""))
                removed = bool(item.get("removed", False))
                file_meta = item.get("file") or {}
                if not file_meta:
                    events.append(RemoteChangeEvent(file_id=file_id, removed=True))
                    continue

                mime_type = str(file_meta.get("mimeType", ""))
                if (
                    mime_type.startswith(GOOGLE_APPS_MIME_PREFIX)
                    and mime_type != FOLDER_MIME_TYPE
                ):
                    continue

                app_props = file_meta.get("appProperties") or {}
                sha256 = str(
                    app_props.get("bearsync_sha256")
                    or file_meta.get("sha256Checksum")
                    or ""
                )
                events.append(
                    RemoteChangeEvent(
                        file_id=file_id,
                        removed=removed or bool(file_meta.get("trashed", False)),
                        name=str(file_meta.get("name", "")),
                        mime_type=mime_type,
                        parents=tuple(file_meta.get("parents") or ()),
                        trashed=bool(file_meta.get("trashed", False)),
                        modified_time=parse_rfc3339_timestamp(file_meta.get("modifiedTime")),
                        sha256=sha256,
                        size=int(file_meta.get("size") or 0),
                    )
                )

            if "newStartPageToken" in response:
                new_start_token = str(response["newStartPageToken"])
            current_token = response.get("nextPageToken")

        return events, new_start_token

    def list_remote_tree(self, root_folder_id: str) -> Dict[str, RemoteChangeEvent]:
        """Perform a breadth-first traversal of `root_folder_id` to build `rel_path -> RemoteChangeEvent`."""
        result: Dict[str, RemoteChangeEvent] = {}
        queue: List[Tuple[str, str]] = [(root_folder_id, "")]

        while queue:
            parent_id, prefix = queue.pop(0)
            page_token: Optional[str] = None
            while True:
                query = f"'{parent_id}' in parents and trashed = false"
                response = (
                    self.service.files()
                    .list(
                        q=query,
                        spaces="drive",
                        fields=f"nextPageToken, files({DRIVE_FILE_FIELDS})",
                        pageSize=200,
                        pageToken=page_token,
                    )
                    .execute()
                )
                for file_meta in response.get("files", []):
                    mime_type = str(file_meta.get("mimeType", ""))
                    if (
                        mime_type.startswith(GOOGLE_APPS_MIME_PREFIX)
                        and mime_type != FOLDER_MIME_TYPE
                    ):
                        continue

                    name = str(file_meta["name"])
                    rel_path = f"{prefix}/{name}".lstrip("/")
                    file_id = str(file_meta["id"])
                    app_props = file_meta.get("appProperties") or {}
                    sha256 = str(
                        app_props.get("bearsync_sha256")
                        or file_meta.get("sha256Checksum")
                        or ""
                    )

                    event = RemoteChangeEvent(
                        file_id=file_id,
                        removed=False,
                        name=name,
                        mime_type=mime_type,
                        parents=tuple(file_meta.get("parents") or ()),
                        trashed=False,
                        modified_time=parse_rfc3339_timestamp(file_meta.get("modifiedTime")),
                        sha256=sha256,
                        size=int(file_meta.get("size") or 0),
                    )
                    result[rel_path] = event

                    if mime_type == FOLDER_MIME_TYPE:
                        queue.append((file_id, rel_path))

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

        return result
