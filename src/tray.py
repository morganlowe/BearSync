"""StatusNotifierItem (SNI) system tray icon and context menu for KDE Plasma 6 Wayland."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

from PyQt6.QtCore import QObject, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices, QIcon, QPainter, QPixmap, QTransform
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from src.app_id import get_assets_dir
from src.sync_engine import SyncState

logger = logging.getLogger(__name__)


class TrayManager(QObject):
    """Manages the FreeDesktop StatusNotifierItem tray icon, animations, and context menu."""

    sync_now_requested = pyqtSignal()
    pause_toggled = pyqtSignal(bool)
    open_folder_requested = pyqtSignal()
    preferences_requested = pyqtSignal()
    about_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(
        self,
        parent_widget: Optional[QWidget] = None,
        notifications_enabled: bool = True,
    ) -> None:
        super().__init__(parent_widget)
        self.notifications_enabled = notifications_enabled
        self._assets_dir = get_assets_dir()
        self._icons: Dict[str, QIcon] = self._load_icons()
        self._sync_frames: list[QIcon] = self._build_sync_animation_frames()
        self._frame_index = 0
        self._is_paused = False
        self._current_state = SyncState.IDLE.value

        self._tray = QSystemTrayIcon(self._icons[SyncState.IDLE.value], self)
        self._tray.setToolTip("BearSync • Idle")

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(180)
        self._anim_timer.timeout.connect(self._advance_sync_animation)

        self._menu = QMenu(parent_widget)
        self._build_context_menu()
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_tray_activated)

    def _load_icons(self) -> Dict[str, QIcon]:
        """Load scalable 22x22 SVG icons for SNI D-Bus rendering on Plasma 6 panels."""
        mapping = {
            SyncState.IDLE.value: "idle.svg",
            SyncState.SYNCING.value: "syncing.svg",
            SyncState.PAUSED.value: "paused.svg",
            SyncState.ERROR.value: "error.svg",
        }
        loaded: Dict[str, QIcon] = {}
        for state_key, filename in mapping.items():
            svg_path = self._assets_dir / filename
            if svg_path.is_file():
                loaded[state_key] = QIcon(str(svg_path))
            else:
                loaded[state_key] = QIcon.fromTheme("folder-cloud")
        return loaded

    def _build_sync_animation_frames(self) -> list[QIcon]:
        """Pre-render subtle opacity/badge frames for smooth low-CPU syncing animation."""
        base_icon = self._icons.get(SyncState.SYNCING.value)
        if base_icon is None or base_icon.isNull():
            return []

        frames: list[QIcon] = [base_icon]
        base_pixmap = base_icon.pixmap(44, 44)
        if base_pixmap.isNull():
            return frames

        for opacity in (0.85, 0.70, 0.85):
            canvas = QPixmap(base_pixmap.size())
            canvas.fill(Qt.GlobalColor.transparent)
            painter = QPainter(canvas)
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.setOpacity(opacity)
            painter.drawPixmap(0, 0, base_pixmap)
            painter.end()
            frames.append(QIcon(canvas))
        return frames

    def _build_context_menu(self) -> None:
        """Build the right-click SNI context menu honoring system QPalette."""
        self._status_action = QAction("Status: Idle • Up to date", self._menu)
        self._status_action.setEnabled(False)
        self._menu.addAction(self._status_action)
        self._menu.addSeparator()

        self._sync_now_action = QAction(
            QIcon.fromTheme("view-refresh"), "Sync Now", self._menu
        )
        self._sync_now_action.triggered.connect(self.sync_now_requested.emit)
        self._menu.addAction(self._sync_now_action)

        self._pause_action = QAction(
            QIcon.fromTheme("media-playback-pause"), "Pause Sync", self._menu
        )
        self._pause_action.triggered.connect(self._on_pause_resume_clicked)
        self._menu.addAction(self._pause_action)

        self._menu.addSeparator()

        self._open_folder_action = QAction(
            QIcon.fromTheme("folder-open"), "Open Folder", self._menu
        )
        self._open_folder_action.triggered.connect(self.open_folder_requested.emit)
        self._menu.addAction(self._open_folder_action)

        self._preferences_action = QAction(
            QIcon.fromTheme("configure"), "Preferences", self._menu
        )
        self._preferences_action.triggered.connect(self.preferences_requested.emit)
        self._menu.addAction(self._preferences_action)

        self._about_action = QAction(
            QIcon.fromTheme("help-about"), "About BearSync", self._menu
        )
        self._about_action.triggered.connect(self.about_requested.emit)
        self._menu.addAction(self._about_action)

        self._menu.addSeparator()

        self._quit_action = QAction(
            QIcon.fromTheme("application-exit"), "Quit", self._menu
        )
        self._quit_action.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(self._quit_action)

    def show(self) -> None:
        """Display the StatusNotifierItem icon in the KDE Plasma system tray."""
        self._tray.show()

    def hide(self) -> None:
        """Hide the system tray icon."""
        self._anim_timer.stop()
        self._tray.hide()

    def update_state(self, state: str, message: str) -> None:
        """Update the tray icon badge, tooltip, and status menu item."""
        self._current_state = state
        self._status_action.setText(f"Status: {message}")
        self._tray.setToolTip(f"BearSync • {message}")

        if state == SyncState.SYNCING.value:
            self._frame_index = 0
            if len(self._sync_frames) > 1:
                self._anim_timer.start()
            self._tray.setIcon(self._icons.get(state, self._icons[SyncState.IDLE.value]))
        else:
            self._anim_timer.stop()
            icon = self._icons.get(state, self._icons[SyncState.IDLE.value])
            self._tray.setIcon(icon)

        if state == SyncState.PAUSED.value:
            self._is_paused = True
            self._pause_action.setText("Resume Sync")
            self._pause_action.setIcon(QIcon.fromTheme("media-playback-start"))
        elif state in (SyncState.IDLE.value, SyncState.SYNCING.value):
            self._is_paused = False
            self._pause_action.setText("Pause Sync")
            self._pause_action.setIcon(QIcon.fromTheme("media-playback-pause"))

    def notify(
        self,
        title: str,
        message: str,
        icon_type: QSystemTrayIcon.MessageIcon = QSystemTrayIcon.MessageIcon.Information,
    ) -> None:
        """Send a desktop notification via KDE Plasma's notification daemon."""
        if not self.notifications_enabled:
            return
        if self._tray.supportsMessages():
            self._tray.showMessage(title, message, icon_type, 4000)

    def _advance_sync_animation(self) -> None:
        if not self._sync_frames:
            return
        self._frame_index = (self._frame_index + 1) % len(self._sync_frames)
        self._tray.setIcon(self._sync_frames[self._frame_index])

    def _on_pause_resume_clicked(self) -> None:
        self._is_paused = not self._is_paused
        self.pause_toggled.emit(self._is_paused)

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.preferences_requested.emit()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.sync_now_requested.emit()


def open_local_folder(folder_path: Path) -> None:
    """Open `folder_path` in Dolphin or the user's default XDG file manager."""
    folder_path.mkdir(parents=True, exist_ok=True)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder_path.resolve())))
