"""Wayland & KDE Plasma 6 desktop environment integration and native QPalette tracking."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, Qt, pyqtSignal
from PyQt6.QtGui import QGuiApplication, QIcon, QPalette
from PyQt6.QtWidgets import QApplication

from src import __app_id__, __app_name__, __version__

logger = logging.getLogger(__name__)

DESKTOP_FILE_NAME = "gdrive-sync"
WAYLAND_APP_ID = __app_id__


def configure_wayland_environment() -> None:
    """Configure Qt environment variables for pure Wayland with XCB fallback on KDE Plasma 6.

    Must be called prior to instantiating QApplication.
    """
    os.environ.setdefault("QT_QPA_PLATFORM", "wayland;xcb")
    os.environ.setdefault("QT_WAYLAND_RECONNECT", "1")
    # Ensure StatusNotifierItem D-Bus tray integration on KDE Plasma / Wayland
    if is_kde_plasma():
        os.environ.setdefault("QT_QPA_PLATFORMTHEME", "kde")
    logger.debug(
        "Configured Wayland environment: QT_QPA_PLATFORM=%s, XDG_SESSION_TYPE=%s, DESKTOP=%s",
        os.environ.get("QT_QPA_PLATFORM"),
        os.environ.get("XDG_SESSION_TYPE"),
        os.environ.get("XDG_CURRENT_DESKTOP"),
    )


def is_wayland_session() -> bool:
    """Return True if running under a Wayland compositor."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    wayland_display = os.environ.get("WAYLAND_DISPLAY", "")
    return session_type == "wayland" or bool(wayland_display)


def is_kde_plasma() -> bool:
    """Return True if running inside KDE Plasma."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    session = os.environ.get("DESKTOP_SESSION", "").lower()
    kde_full = os.environ.get("KDE_FULL_SESSION", "").lower()
    return "kde" in desktop or "plasma" in desktop or "plasma" in session or kde_full == "true"


def get_assets_dir() -> Path:
    """Locate the application's SVG assets directory."""
    candidates = [
        Path(__file__).resolve().parent.parent / "assets",
        Path("/usr/share/gdrive-sync/assets"),
        Path.home() / ".local" / "share" / "gdrive-sync" / "assets",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def apply_application_identity(app: QApplication) -> None:
    """Bind Wayland app_id, StartupWMClass, and native High-DPI attributes to QApplication."""
    app.setApplicationName(__app_name__)
    app.setApplicationDisplayName("BearSync (Google Drive)")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("BearSync")
    app.setOrganizationDomain("bearsync.org")

    # Critical for Wayland compositor taskbar icon mapping, window rules, and notifications.
    # Matches StartupWMClass=org.bearsync.gdrive-sync and gdrive-sync.desktop
    QGuiApplication.setDesktopFileName(WAYLAND_APP_ID)

    icon_path = get_assets_dir() / "gdrive-sync.svg"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


class PortalPaletteWatcher(QObject):
    """Monitors FreeDesktop portal (`org.freedesktop.portal.Settings`) and Qt6 style hints.

    Ensures the application adheres strictly to KDE Plasma 6 system dark/light modes
    and accent colors natively via `QPalette` without hardcoded CSS stylesheets.
    """

    theme_changed = pyqtSignal(QPalette)

    def __init__(self, app: QApplication, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._app = app
        self._dbus_connected = False

        # Connect native Qt 6.5+ QStyleHints colorSchemeChanged signal
        style_hints = app.styleHints()
        if style_hints is not None and hasattr(style_hints, "colorSchemeChanged"):
            style_hints.colorSchemeChanged.connect(self._on_qt_color_scheme_changed)

        app.paletteChanged.connect(self._on_palette_changed)
        self._subscribe_freedesktop_portal()

    def _subscribe_freedesktop_portal(self) -> None:
        """Connect to org.freedesktop.portal.Settings.SettingChanged over session D-Bus."""
        try:
            from PyQt6.QtDBus import QDBusConnection

            bus = QDBusConnection.sessionBus()
            if not bus.isConnected():
                logger.debug("D-Bus session bus not available for portal settings monitoring.")
                return

            connected = bus.connect(
                "org.freedesktop.portal.Desktop",
                "/org/freedesktop/portal/desktop",
                "org.freedesktop.portal.Settings",
                "SettingChanged",
                self._on_portal_setting_changed,
            )
            self._dbus_connected = bool(connected)
            logger.debug("Subscribed to org.freedesktop.portal.Settings: %s", self._dbus_connected)
        except Exception as exc:
            logger.debug("Optional QtDBus portal listener unavailable: %s", exc)

    def _on_portal_setting_changed(self, namespace: str, key: str, value: object) -> None:
        """Handle live KDE Plasma / xdg-desktop-portal appearance changes."""
        if namespace == "org.freedesktop.appearance" and key in ("color-scheme", "accent-color"):
            logger.info("FreeDesktop portal appearance changed: %s.%s = %s", namespace, key, value)
            self._refresh_widgets()

    def _on_qt_color_scheme_changed(self, scheme: Qt.ColorScheme) -> None:
        logger.info("Qt color scheme changed to: %s", scheme)
        self._refresh_widgets()

    def _on_palette_changed(self, palette: QPalette) -> None:
        self.theme_changed.emit(palette)

    def _refresh_widgets(self) -> None:
        """Notify all top-level widgets to re-polish using the updated native system QPalette."""
        palette = self._app.palette()
        for widget in self._app.topLevelWidgets():
            style = widget.style()
            if style is not None:
                style.unpolish(widget)
                style.polish(widget)
            widget.update()
        self.theme_changed.emit(palette)
