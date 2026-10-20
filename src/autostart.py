"""Manage XDG Autostart desktop entry for BearSync."""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "bearsync.desktop"


def set_autostart_enabled(enabled: bool) -> bool:
    """Enable or disable autostarting BearSync at user login."""
    try:
        if not enabled:
            if AUTOSTART_FILE.exists():
                AUTOSTART_FILE.unlink()
                logger.info("Removed autostart desktop entry at %s", AUTOSTART_FILE)
            return True

        AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        exec_path = shutil.which("bearsync") or shutil.which("gdrive-sync")
        if not exec_path:
            exec_path = f"{sys.executable} -m src.main"

        content = f"""[Desktop Entry]
Type=Application
Version=1.5
Name=BearSync
GenericName=Google Drive Sync
Comment=Wayland & KDE Plasma Google Drive Sync Client
Exec={exec_path} --minimized
Icon=gdrive-sync
Terminal=false
Categories=Network;FileTransfer;
X-GNOME-Autostart-enabled=true
StartupNotify=false
"""
        AUTOSTART_FILE.write_text(content, encoding="utf-8")
        logger.info("Wrote autostart desktop entry to %s", AUTOSTART_FILE)
        return True
    except Exception as exc:
        logger.warning("Failed to update autostart configuration: %s", exc)
        return False


def is_autostart_enabled() -> bool:
    """Check if the autostart desktop entry is present."""
    return AUTOSTART_FILE.is_file()
