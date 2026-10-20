# BearSync Privacy Policy

**Last Updated:** October 19, 2026

BearSync (`bearsync` / `gdrive-sync`) is an open-source, local desktop application for synchronizing folders between your Linux computer and your personal Google Drive account.

## 1. Data Collection & Storage
- **No External Servers or Telemetry:** BearSync runs entirely on your local Linux machine and communicates directly with Google's official Google Drive API v3 (`https://www.googleapis.com/drive/v3`). BearSync does not operate any intermediate servers, analytics, telemetry, or tracking.
- **OAuth2 Credentials & Tokens:** Your Google OAuth2 authorization token is stored locally on your machine at `~/.config/gdrive-sync/token.json` with strict `0600` (owner-only) filesystem permissions.
- **Local Sync Cache:** File metadata (relative paths, SHA-256 checksums, modification timestamps, and Google Drive file IDs) is cached locally in `~/.config/gdrive-sync/sync_cache.db`.

## 2. How Google User Data Is Used
BearSync requests the `https://www.googleapis.com/auth/drive` scope solely to:
1. List, create, upload, download, update, and move to trash files and folders within the specific Google Drive directories you configure for synchronization.
2. Read your Google Drive account display name and email address (`about.get`) to display which account is currently connected in the BearSync user interface.

BearSync never shares, sells, or transmits your Google user data to any third party.

## 3. Revoking Access & Deleting Data
You can disconnect your Google account at any time within BearSync, delete `~/.config/gdrive-sync/`, or revoke BearSync's access directly from your [Google Account Permissions page](https://myaccount.google.com/permissions).
