"""Application entrypoint, CLI flags, single-instance IPC, and Wayland/KDE Plasma lifecycle."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from PyQt6.QtCore import QObject
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from src import __app_id__, __version__
from src.app_id import (
    PortalPaletteWatcher,
    apply_application_identity,
    configure_wayland_environment,
)
from src.db import SyncDatabase
from src.drive_api import DriveClient
from src.sync_engine import SyncController, SyncEngine
from src.tray import TrayManager, open_local_folder
from src.ui.preferences_dialog import PreferencesDialog
from src.ui.setup_wizard import SetupWizard
from src.versioning import VersioningEngine
from src.watcher import LocalFolderWatcher

logger = logging.getLogger(__name__)

IPC_SOCKET_NAME = f"{__app_id__}.ipc"


class BearSyncApp(QObject):
    """Primary application coordinator tying together Wayland UI, SNI Tray, Multi-Watcher, and SyncEngine."""

    def __init__(self, app: QApplication, args: argparse.Namespace) -> None:
        super().__init__()
        self.app = app
        self.args = args
        self._quitting = False

        # Keep daemon running when Preferences or Setup Wizard dialogs are closed to tray
        self.app.setQuitOnLastWindowClosed(False)

        self.palette_watcher = PortalPaletteWatcher(self.app, self)
        self.db = SyncDatabase()
        self.settings = self.db.get_settings()

        sync_root = Path(str(self.settings["sync_folder"])).expanduser().resolve()

        self.versioning = VersioningEngine(
            sync_root=sync_root,
            monitored_paths=self.settings.get("versioning_paths", ["*"]),
            enabled=bool(self.settings.get("versioning_enabled", True)),
            mode=str(self.settings.get("versioning_mode", "versions_subdir")),
            max_versions_per_file=int(self.settings.get("max_versions_per_file", 10)),
        )
        self.drive = DriveClient()
        self.watcher = LocalFolderWatcher(
            sync_root=sync_root,
            debounce_seconds=float(self.settings.get("debounce_seconds", 3.0)),
            parent=self,
        )
        self.watcher.set_sync_pairs(
            self.db.get_sync_pairs(),
            debounce_seconds=float(self.settings.get("debounce_seconds", 3.0)),
        )

        self.engine = SyncEngine(
            db=self.db,
            drive=self.drive,
            versioning=self.versioning,
            sync_root=sync_root,
            suppress_watcher_cb=self.watcher.suppress_path,
        )
        self.controller = SyncController(
            engine=self.engine,
            poll_interval_seconds=int(self.settings.get("poll_interval_seconds", 60)),
            parent=self,
        )

        self.preferences_dialog = PreferencesDialog(
            db=self.db,
            drive=self.drive,
            allow_quit_cb=lambda: self._quitting,
        )
        self.setup_wizard = SetupWizard(
            db=self.db,
            drive=self.drive,
            allow_quit_cb=lambda: self._quitting,
        )
        self.tray = TrayManager(
            parent_widget=self.preferences_dialog,
            notifications_enabled=bool(self.settings.get("notifications_enabled", True)),
        )

        self._ipc_server = QLocalServer(self)
        self._setup_connections()
        self._start_ipc_server()

    def _setup_connections(self) -> None:
        # Multi-pair Watcher -> Controller
        self.watcher.pair_changes_detected.connect(
            self.controller.enqueue_pair_local_changes
        )

        # Controller -> Tray & live progress panel
        self.controller.state_changed.connect(self.tray.update_state)
        self.controller.state_changed.connect(self.preferences_dialog.on_sync_state)
        self.controller.progress_updated.connect(self.preferences_dialog.on_sync_progress)
        self.controller.activity_logged.connect(self.preferences_dialog.append_activity)
        self.controller.conflict_detected.connect(self._on_conflicts_resolved)

        # Tray actions
        self.tray.sync_now_requested.connect(self.controller.request_full_sync)
        self.tray.pause_toggled.connect(self.controller.set_paused)
        self.tray.open_folder_requested.connect(self.open_sync_folder)
        self.tray.preferences_requested.connect(self.show_preferences)
        self.tray.about_requested.connect(self.show_about)
        self.tray.quit_requested.connect(self.quit)

        # Dialog & Wizard signals
        self.preferences_dialog.settings_saved.connect(self._apply_updated_settings)
        self.preferences_dialog.run_wizard_requested.connect(self.show_setup_wizard)
        self.preferences_dialog.sync_pair_now_requested.connect(
            self.controller.request_full_sync
        )
        self.preferences_dialog.closed_to_tray.connect(
            lambda: self.tray.notify(
                "BearSync Running in Background",
                "BearSync continues synchronizing your folders in the system tray.",
            )
        )
        self.setup_wizard.setup_completed.connect(self._on_setup_completed)

    def _start_ipc_server(self) -> None:
        QLocalServer.removeServer(IPC_SOCKET_NAME)
        if self._ipc_server.listen(IPC_SOCKET_NAME):
            self._ipc_server.newConnection.connect(self._on_ipc_connection)

    def _on_ipc_connection(self) -> None:
        conn = self._ipc_server.nextPendingConnection()
        if conn is None:
            return
        if conn.waitForReadyRead(500):
            cmd = bytes(conn.readAll()).decode("utf-8", errors="ignore").strip()
            self._handle_ipc_command(cmd)
        conn.disconnectFromServer()

    def _handle_ipc_command(self, cmd: str) -> None:
        logger.info("Received IPC command: %s", cmd)
        if cmd == "sync-now":
            self.controller.request_full_sync()
        elif cmd == "open-folder":
            self.open_sync_folder()
        elif cmd == "preferences":
            self.show_preferences()
        elif cmd == "setup":
            self.show_setup_wizard()

    def start(self) -> None:
        """Start the SNI tray icon and either launch the Setup Wizard or begin background sync."""
        self.tray.show()

        if self.args.open_folder:
            self.open_sync_folder()
        if self.args.preferences:
            self.show_preferences()

        needs_setup = (
            self.args.setup
            or not bool(self.settings.get("setup_completed", False))
            or not self.drive.has_valid_token()
        )
        if needs_setup:
            self.tray.update_state("paused", "Account setup required")
            if not self.args.minimized:
                self.show_setup_wizard()
            return

        self._start_sync_services()
        if self.args.sync_now:
            self.controller.request_full_sync()

    def _start_sync_services(self) -> None:
        self.watcher.set_sync_pairs(
            self.db.get_sync_pairs(),
            debounce_seconds=float(self.settings.get("debounce_seconds", 3.0)),
        )
        self.watcher.start()
        self.controller.start()

    def _on_setup_completed(self, new_settings: Dict[str, Any]) -> None:
        self.preferences_dialog.load_from_db()
        self._apply_updated_settings(new_settings)
        if self.drive.has_valid_token():
            self._start_sync_services()
        self.show_preferences()

    def _apply_updated_settings(self, _new_settings: Dict[str, Any]) -> None:
        self.settings = self.db.get_settings()
        pairs = self.db.get_sync_pairs()

        new_root = Path(str(self.settings["sync_folder"])).expanduser().resolve()
        self.engine.sync_root = new_root
        self.versioning.sync_root = new_root
        self.versioning.enabled = bool(self.settings.get("versioning_enabled", True))
        self.versioning.mode = str(self.settings.get("versioning_mode", "versions_subdir"))
        self.versioning.max_versions_per_file = int(
            self.settings.get("max_versions_per_file", 10)
        )
        self.versioning.set_monitored_paths(
            self.settings.get("versioning_paths", ["*"])
        )

        self.tray.notifications_enabled = bool(
            self.settings.get("notifications_enabled", True)
        )
        self.watcher.set_sync_pairs(
            pairs, float(self.settings.get("debounce_seconds", 3.0))
        )
        self.controller.set_poll_interval(
            int(self.settings.get("poll_interval_seconds", 60))
        )
        logger.info("Applied updated settings (%d sync pair(s) configured)", len(pairs))

    def _on_conflicts_resolved(self, count: int) -> None:
        self.tray.notify(
            "Sync Conflict Resolved",
            f"Preserved {count} conflicting file(s) with .conflict.[timestamp] suffix.",
            QSystemTrayIcon.MessageIcon.Warning,
        )

    def open_sync_folder(self) -> None:
        pairs = self.db.get_sync_pairs()
        if pairs:
            open_local_folder(Path(pairs[0].local_path).expanduser().resolve())
        else:
            sync_root = Path(str(self.settings["sync_folder"])).expanduser().resolve()
            open_local_folder(sync_root)

    def show_preferences(self) -> None:
        self.preferences_dialog.load_from_db()
        self.preferences_dialog.show()
        self.preferences_dialog.raise_()
        self.preferences_dialog.activateWindow()

    def show_about(self) -> None:
        self.preferences_dialog.load_from_db()
        self.preferences_dialog.show_about_tab()
        self.preferences_dialog.show()
        self.preferences_dialog.raise_()
        self.preferences_dialog.activateWindow()

    def show_setup_wizard(self) -> None:
        self.setup_wizard.show()
        self.setup_wizard.raise_()
        self.setup_wizard.activateWindow()

    def quit(self) -> None:
        """Cleanly stop inotify watcher, sync thread, SQLite connection, and exit."""
        if self._quitting:
            return
        self._quitting = True
        logger.info("Shutting down BearSync...")
        self.watcher.stop()
        self.controller.stop()
        self.tray.hide()
        self._ipc_server.close()
        QLocalServer.removeServer(IPC_SOCKET_NAME)
        self.db.close()
        self.app.quit()


def send_ipc_command_if_running(command: str) -> bool:
    """Attempt to forward a CLI command to an already-running BearSync instance."""
    socket = QLocalSocket()
    socket.connectToServer(IPC_SOCKET_NAME)
    if not socket.waitForConnected(250):
        return False
    if command:
        socket.write(command.encode("utf-8"))
        socket.waitForBytesWritten(500)
    socket.disconnectFromServer()
    return True


def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bearsync",
        description="BearSync — Native Wayland & KDE Plasma 6 multi-folder bi-directional Google Drive sync client",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--minimized",
        action="store_true",
        help="Start quietly in the KDE Plasma system tray without opening modals",
    )
    parser.add_argument(
        "--sync-now",
        action="store_true",
        help="Trigger an immediate full bi-directional sync pass across all folder pairs",
    )
    parser.add_argument(
        "--open-folder",
        action="store_true",
        help="Open the local sync directory in the file manager",
    )
    parser.add_argument(
        "--preferences",
        action="store_true",
        help="Open the BearSync Account & Folder Sync window",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Launch the OAuth2 Account & Folder Setup Wizard",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    configure_wayland_environment()

    ipc_cmd = ""
    if args.sync_now:
        ipc_cmd = "sync-now"
    elif args.open_folder:
        ipc_cmd = "open-folder"
    elif args.preferences:
        ipc_cmd = "preferences"
    elif args.setup:
        ipc_cmd = "setup"

    if send_ipc_command_if_running(ipc_cmd or "preferences"):
        logger.info(
            "Forwarded command '%s' to running BearSync instance.",
            ipc_cmd or "preferences",
        )
        return 0

    app = QApplication(sys.argv if argv is None else ["bearsync", *argv])
    apply_application_identity(app)

    client = BearSyncApp(app, args)

    signal.signal(signal.SIGINT, lambda *_: client.quit())
    signal.signal(signal.SIGTERM, lambda *_: client.quit())

    client.start()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
