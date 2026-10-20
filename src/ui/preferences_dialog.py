"""Nextcloud-style main settings & multi-folder sync management window for BearSync."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from PyQt6.QtCore import QEvent, Qt, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QIcon, QPainter, QPalette, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src import __version__
from src.app_id import get_assets_dir
from src.autostart import set_autostart_enabled
from src.db import SyncDatabase, SyncPair
from src.drive_api import DriveClient
from src.tray import open_local_folder
from src.ui.folder_pair_dialog import SyncPairDialog

logger = logging.getLogger(__name__)


class PreferencesDialog(QDialog):
    """Nextcloud-style Account & Multi-Folder Sync window styled via system `QPalette`.

    Implements 'Close to Tray' by overriding `closeEvent`.
    """

    settings_saved = pyqtSignal(dict)
    run_wizard_requested = pyqtSignal()
    sync_pair_now_requested = pyqtSignal(str)
    closed_to_tray = pyqtSignal()

    def __init__(
        self,
        db: SyncDatabase,
        drive: Optional[DriveClient] = None,
        parent: Optional[QWidget] = None,
        allow_quit_cb: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.drive = drive or DriveClient()
        self._allow_quit_cb = allow_quit_cb
        self._notified_close_to_tray = False
        self._pairs: List[SyncPair] = []
        self._pair_status: Dict[str, str] = {}

        self.setWindowTitle("BearSync — Account & Folder Sync")
        self.setMinimumSize(760, 520)
        self.setModal(False)

        self._build_ui()
        self.load_from_db()

    def _build_ui(self) -> None:
        root_layout = QVBoxLayout(self)
        root_layout.setSpacing(12)

        # --- Top Account Banner (Nextcloud Style) ---
        account_group = QGroupBox("Google Drive Account", self)
        account_layout = QHBoxLayout(account_group)

        self.account_status_label = QLabel("Account: Checking...", self)
        account_layout.addWidget(self.account_status_label, stretch=1)

        wizard_btn = QPushButton(
            QIcon.fromTheme("preferences-desktop-user-password"),
            "Account Setup / Re-authenticate...",
            self,
        )
        wizard_btn.clicked.connect(self.run_wizard_requested.emit)
        account_layout.addWidget(wizard_btn)

        root_layout.addWidget(account_group)

        # --- Main Tabs ---
        self.tabs = QTabWidget(self)
        self.tabs.addTab(self._build_folders_tab(), "Folder Sync Connections")
        self.tabs.addTab(self._build_options_tab(), "Options")
        self.tabs.addTab(self._build_about_tab(), "About")
        root_layout.addWidget(self.tabs, stretch=1)

        # --- Bottom Dialog Buttons ---
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Close,
            Qt.Orientation.Horizontal,
            self,
        )
        self.button_box.accepted.connect(self._on_save_clicked)
        self.button_box.rejected.connect(self.hide)
        root_layout.addWidget(self.button_box)

    def _build_folders_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)

        desc = QLabel(
            "Manage your bi-directional Local ↔ Google Drive folder sync connections. "
            "You can sync any local directory to any folder or subfolder path in Google Drive.",
            tab,
        )
        desc.setWordWrap(True)
        layout.addWidget(desc)

        self.pairs_table = QTableWidget(0, 5, tab)
        self.pairs_table.setHorizontalHeaderLabels(
            ["Active", "Local Folder", "Google Drive Folder", "Versioning", "Status"]
        )
        header = self.pairs_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.pairs_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.pairs_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.pairs_table.itemDoubleClicked.connect(lambda *_: self._edit_selected_pair())
        self.pairs_table.itemChanged.connect(self._on_table_item_changed)
        layout.addWidget(self.pairs_table, stretch=1)

        # Action toolbar
        btn_row = QHBoxLayout()
        add_btn = QPushButton(
            QIcon.fromTheme("list-add"), "+ Add Folder Sync Connection...", tab
        )
        add_btn.clicked.connect(self._add_sync_pair)

        edit_btn = QPushButton(
            QIcon.fromTheme("document-edit"), "Edit Connection...", tab
        )
        edit_btn.clicked.connect(self._edit_selected_pair)

        remove_btn = QPushButton(
            QIcon.fromTheme("list-remove"), "Remove", tab
        )
        remove_btn.clicked.connect(self._remove_selected_pair)

        open_local_btn = QPushButton(
            QIcon.fromTheme("folder-open"), "Open Local Folder", tab
        )
        open_local_btn.clicked.connect(self._open_selected_local_folder)

        sync_pair_btn = QPushButton(
            QIcon.fromTheme("view-refresh"), "Sync Selected Now", tab
        )
        sync_pair_btn.clicked.connect(self._sync_selected_pair_now)

        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addWidget(open_local_btn)
        btn_row.addWidget(sync_pair_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        # --- Live Sync Progress & Activity Log ---
        progress_group = QGroupBox("Sync Activity", tab)
        progress_layout = QVBoxLayout(progress_group)

        self.sync_status_label = QLabel("Idle", progress_group)
        self.sync_status_label.setWordWrap(True)
        progress_layout.addWidget(self.sync_status_label)

        self.sync_progress_bar = QProgressBar(progress_group)
        self.sync_progress_bar.setRange(0, 1)
        self.sync_progress_bar.setValue(1)
        self.sync_progress_bar.setTextVisible(True)
        progress_layout.addWidget(self.sync_progress_bar)

        self.activity_log = QPlainTextEdit(progress_group)
        self.activity_log.setReadOnly(True)
        self.activity_log.setMaximumBlockCount(500)
        self.activity_log.setPlaceholderText("Uploads, downloads, and conflicts will appear here.")
        self.activity_log.setMinimumHeight(110)
        progress_layout.addWidget(self.activity_log)

        layout.addWidget(progress_group)

        return tab

    # --- Live progress slots (connected from SyncController in main.py) ---

    def on_sync_state(self, state: str, message: str) -> None:
        """Reflect the overall controller state in the activity panel."""
        self.sync_status_label.setText(message)
        if state != "syncing":
            self.sync_progress_bar.setRange(0, 1)
            self.sync_progress_bar.setValue(1)
            self.sync_progress_bar.setFormat("Up to date" if state == "idle" else state.capitalize())

    def on_sync_progress(self, pair_id: str, detail: str, current: int, total: int) -> None:
        """Update the per-folder status column and the progress bar."""
        self.sync_status_label.setText(detail)
        if total > 0:
            self.sync_progress_bar.setRange(0, total)
            self.sync_progress_bar.setValue(min(current, total))
            self.sync_progress_bar.setFormat("%v / %m files")
        else:
            # Indeterminate (busy) while scanning / listing
            self.sync_progress_bar.setRange(0, 0)

        if detail == "Up to date ✓":
            status = "Up to date ✓"
        elif total > 0:
            status = f"Syncing {current}/{total}"
        else:
            status = "Scanning..."
        if self._pair_status.get(pair_id) != status:
            self._pair_status[pair_id] = status
            self._update_status_cell(pair_id)

    def append_activity(self, line: str) -> None:
        self.activity_log.appendPlainText(line)

    def _update_status_cell(self, pair_id: str) -> None:
        for row, pair in enumerate(self._pairs):
            if pair.pair_id == pair_id:
                self.pairs_table.blockSignals(True)
                self.pairs_table.setItem(
                    row, 4, QTableWidgetItem(self._pair_status.get(pair_id, "—"))
                )
                self.pairs_table.blockSignals(False)
                break

    def _build_options_tab(self) -> QWidget:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        container = QWidget(scroll)
        layout = QVBoxLayout(container)
        layout.setSpacing(14)
        layout.setContentsMargins(6, 6, 6, 6)

        # 1. Bandwidth Limits (Rate Limiting)
        bw_group = QGroupBox("Bandwidth Limits (Rate Limiting)", container)
        bw_form = QFormLayout(bw_group)
        bw_form.setSpacing(8)

        self.upload_limit_spin = QSpinBox(bw_group)
        self.upload_limit_spin.setRange(0, 1000000)
        self.upload_limit_spin.setSingleStep(100)
        self.upload_limit_spin.setSuffix(" KB/s")
        self.upload_limit_spin.setSpecialValueText("Unlimited (0 KB/s)")
        bw_form.addRow("Maximum Upload Rate:", self.upload_limit_spin)

        self.download_limit_spin = QSpinBox(bw_group)
        self.download_limit_spin.setRange(0, 1000000)
        self.download_limit_spin.setSingleStep(100)
        self.download_limit_spin.setSuffix(" KB/s")
        self.download_limit_spin.setSpecialValueText("Unlimited (0 KB/s)")
        bw_form.addRow("Maximum Download Rate:", self.download_limit_spin)

        layout.addWidget(bw_group)

        # 2. Network & Proxy Configuration
        proxy_group = QGroupBox("Network & Proxy", container)
        proxy_layout = QVBoxLayout(proxy_group)
        proxy_layout.setSpacing(8)

        proxy_mode_layout = QHBoxLayout()
        proxy_mode_label = QLabel("Proxy Mode:", proxy_group)
        self.proxy_mode_combo = QComboBox(proxy_group)
        self.proxy_mode_combo.addItem("System Default (Auto-detect from environment)", "system")
        self.proxy_mode_combo.addItem("No Proxy (Direct Connection)", "none")
        self.proxy_mode_combo.addItem("Manual Proxy Configuration", "manual")
        self.proxy_mode_combo.currentIndexChanged.connect(self._on_proxy_mode_changed)
        proxy_mode_layout.addWidget(proxy_mode_label)
        proxy_mode_layout.addWidget(self.proxy_mode_combo, stretch=1)
        proxy_layout.addLayout(proxy_mode_layout)

        self.proxy_manual_widget = QWidget(proxy_group)
        manual_form = QFormLayout(self.proxy_manual_widget)
        manual_form.setContentsMargins(0, 4, 0, 0)
        manual_form.setSpacing(8)

        self.proxy_host_edit = QLineEdit(self.proxy_manual_widget)
        self.proxy_host_edit.setPlaceholderText("e.g. proxy.example.com or 127.0.0.1")
        manual_form.addRow("Proxy Host:", self.proxy_host_edit)

        self.proxy_port_spin = QSpinBox(self.proxy_manual_widget)
        self.proxy_port_spin.setRange(1, 65535)
        self.proxy_port_spin.setValue(8080)
        manual_form.addRow("Proxy Port:", self.proxy_port_spin)

        self.proxy_user_edit = QLineEdit(self.proxy_manual_widget)
        self.proxy_user_edit.setPlaceholderText("Optional authentication username")
        manual_form.addRow("Proxy Username:", self.proxy_user_edit)

        self.proxy_pass_edit = QLineEdit(self.proxy_manual_widget)
        self.proxy_pass_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.proxy_pass_edit.setPlaceholderText("Optional authentication password")
        manual_form.addRow("Proxy Password:", self.proxy_pass_edit)

        proxy_layout.addWidget(self.proxy_manual_widget)
        self.proxy_manual_widget.setVisible(False)

        layout.addWidget(proxy_group)

        # 3. Sync & Conflict Rules
        sync_group = QGroupBox("Sync & Conflict Rules", container)
        sync_form = QFormLayout(sync_group)
        sync_form.setSpacing(8)

        self.poll_spin = QSpinBox(sync_group)
        self.poll_spin.setRange(15, 86400)
        self.poll_spin.setSingleStep(15)
        self.poll_spin.setSuffix(" seconds")
        sync_form.addRow("Remote Poll Interval:", self.poll_spin)

        self.debounce_spin = QDoubleSpinBox(sync_group)
        self.debounce_spin.setRange(0.5, 30.0)
        self.debounce_spin.setSingleStep(0.5)
        self.debounce_spin.setDecimals(1)
        self.debounce_spin.setSuffix(" seconds")
        sync_form.addRow("Linux Inotify Write Debounce:", self.debounce_spin)

        self.max_file_size_spin = QSpinBox(sync_group)
        self.max_file_size_spin.setRange(0, 500000)
        self.max_file_size_spin.setSingleStep(100)
        self.max_file_size_spin.setSuffix(" MB")
        self.max_file_size_spin.setSpecialValueText("Unlimited (0 MB)")
        sync_form.addRow("Maximum File Size:", self.max_file_size_spin)

        self.conflict_strategy_combo = QComboBox(sync_group)
        self.conflict_strategy_combo.addItem(
            "Keep both files (Create .conflict copy)", "keep_both"
        )
        self.conflict_strategy_combo.addItem(
            "Prefer local file (Overwrite remote)", "keep_local"
        )
        self.conflict_strategy_combo.addItem(
            "Prefer remote file (Overwrite local)", "keep_remote"
        )
        sync_form.addRow("Conflict Resolution:", self.conflict_strategy_combo)

        layout.addWidget(sync_group)

        # 4. System Integration
        sys_group = QGroupBox("System Integration", container)
        sys_layout = QVBoxLayout(sys_group)
        sys_layout.setSpacing(6)

        self.autostart_check = QCheckBox(
            "Launch BearSync automatically at system startup", sys_group
        )
        sys_layout.addWidget(self.autostart_check)

        self.start_minimized_check = QCheckBox(
            "Start minimized in KDE Plasma system tray", sys_group
        )
        sys_layout.addWidget(self.start_minimized_check)

        self.notifications_check = QCheckBox(
            "Show KDE Plasma desktop notifications on conflicts or errors", sys_group
        )
        sys_layout.addWidget(self.notifications_check)

        layout.addWidget(sys_group)
        layout.addStretch(1)

        scroll.setWidget(container)
        return scroll

    def _on_proxy_mode_changed(self) -> None:
        is_manual = self.proxy_mode_combo.currentData() == "manual"
        self.proxy_manual_widget.setVisible(is_manual)

    def _build_about_tab(self) -> QWidget:
        tab = QWidget(self)
        layout = QVBoxLayout(tab)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.about_logo_label = QLabel(tab)
        self.about_logo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._update_bear_built_logo()
        layout.addWidget(self.about_logo_label)

        title_label = QLabel(f"<h2>BearSync v{__version__}</h2>", tab)
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        subtitle_label = QLabel(
            "<b>Bear Built</b> — Native Wayland &amp; KDE Plasma 6 Bi-Directional Google Drive Sync Client<br>"
            "Designed for CachyOS, Arch Linux, and Debian",
            tab,
        )
        subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

        features_label = QLabel(
            "• Multi-folder Local ↔ Google Drive synchronization with remote subfolder creation<br>"
            "• Linux inotify real-time watcher with 3-second write debounce<br>"
            "• Differential Google Drive API v3 polling &amp; automatic conflict preservation<br>"
            "• Custom pre-overwrite timestamp versioning (_YYYYMMDD_HHMMSS)<br>"
            "• License: GNU GPL v3.0",
            tab,
        )
        features_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(features_label)

        layout.addStretch(1)
        return tab

    def _update_bear_built_logo(self) -> None:
        """Render the Bear-Built logo adapted to the current KDE Plasma light/dark QPalette."""
        if not hasattr(self, "about_logo_label"):
            return
        logo_path = get_assets_dir() / "bear-built-logo.png"
        if not logo_path.is_file():
            return

        raw_pixmap = QPixmap(str(logo_path))
        if raw_pixmap.isNull():
            return

        scaled = raw_pixmap.scaled(
            180,
            180,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        tinted = QPixmap(scaled.size())
        tinted.fill(Qt.GlobalColor.transparent)

        painter = QPainter(tinted)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(0, 0, scaled)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(
            tinted.rect(), self.palette().color(QPalette.ColorRole.WindowText)
        )
        painter.end()

        self.about_logo_label.setPixmap(tinted)

    def show_about_tab(self) -> None:
        """Switch directly to the About tab."""
        self._update_bear_built_logo()
        self.tabs.setCurrentIndex(2)

    def changeEvent(self, event: QEvent) -> None:
        if event.type() == QEvent.Type.PaletteChange:
            self._update_bear_built_logo()
        super().changeEvent(event)

    def load_from_db(self) -> None:
        """Populate account banner, folder sync connections table, and schedule settings."""
        settings = self.db.get_settings()
        email = str(settings.get("account_email") or "")
        name = str(settings.get("account_name") or "")

        if self.drive.has_valid_token():
            if not email:
                info = self.drive.get_account_info()
                email = str(info.get("email") or "")
                name = str(info.get("display_name") or "")
                if email:
                    self.db.save_settings({"account_email": email, "account_name": name})
            if email:
                self.account_status_label.setText(
                    f"Connected to Google Drive as: {name} ({email})"
                )
            else:
                self.account_status_label.setText("Connected to Google Drive ✓")
        else:
            self.account_status_label.setText(
                "Not connected — Click 'Account Setup / Re-authenticate...' to sign in."
            )

        self._pairs = self.db.get_sync_pairs()
        self._refresh_pairs_table()

        self.poll_spin.setValue(int(settings.get("poll_interval_seconds", 60)))
        self.debounce_spin.setValue(float(settings.get("debounce_seconds", 3.0)))
        self.notifications_check.setChecked(
            bool(settings.get("notifications_enabled", True))
        )
        self.upload_limit_spin.setValue(int(settings.get("rate_limit_upload_kbps", 0)))
        self.download_limit_spin.setValue(int(settings.get("rate_limit_download_kbps", 0)))

        proxy_mode = str(settings.get("proxy_mode", "system"))
        p_idx = self.proxy_mode_combo.findData(proxy_mode)
        self.proxy_mode_combo.setCurrentIndex(p_idx if p_idx >= 0 else 0)
        self.proxy_host_edit.setText(str(settings.get("proxy_host", "")))
        self.proxy_port_spin.setValue(int(settings.get("proxy_port", 8080)))
        self.proxy_user_edit.setText(str(settings.get("proxy_user", "")))
        self.proxy_pass_edit.setText(str(settings.get("proxy_pass", "")))
        self._on_proxy_mode_changed()

        self.max_file_size_spin.setValue(int(settings.get("max_file_size_mb", 0)))
        conflict_strategy = str(settings.get("conflict_strategy", "keep_both"))
        c_idx = self.conflict_strategy_combo.findData(conflict_strategy)
        self.conflict_strategy_combo.setCurrentIndex(c_idx if c_idx >= 0 else 0)

        self.autostart_check.setChecked(bool(settings.get("autostart", True)))
        self.start_minimized_check.setChecked(bool(settings.get("start_minimized", False)))

    def _refresh_pairs_table(self) -> None:
        self.pairs_table.blockSignals(True)
        self.pairs_table.setRowCount(0)
        for pair in self._pairs:
            row = self.pairs_table.rowCount()
            self.pairs_table.insertRow(row)

            chk_item = QTableWidgetItem()
            chk_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsSelectable
            )
            chk_item.setCheckState(
                Qt.CheckState.Checked if pair.enabled else Qt.CheckState.Unchecked
            )
            self.pairs_table.setItem(row, 0, chk_item)
            self.pairs_table.setItem(row, 1, QTableWidgetItem(pair.local_path))
            self.pairs_table.setItem(row, 2, QTableWidgetItem(pair.remote_path))

            ver_label = (
                f"ON ({'.versions/' if pair.versioning_mode == 'versions_subdir' else 'inline'})"
                if pair.versioning_enabled
                else "OFF"
            )
            self.pairs_table.setItem(row, 3, QTableWidgetItem(ver_label))
            status = self._pair_status.get(pair.pair_id) or (
                "Waiting..." if pair.enabled else "Paused"
            )
            self.pairs_table.setItem(row, 4, QTableWidgetItem(status))
        self.pairs_table.blockSignals(False)

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            row = item.row()
            if 0 <= row < len(self._pairs):
                self._pairs[row].enabled = (
                    item.checkState() == Qt.CheckState.Checked
                )

    def _add_sync_pair(self) -> None:
        dlg = SyncPairDialog(self.drive, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_pair is not None:
            self._pairs.append(dlg.result_pair)
            self._refresh_pairs_table()
            self._persist_current_state()

    def _edit_selected_pair(self) -> None:
        row = self.pairs_table.currentRow()
        if row < 0 or row >= len(self._pairs):
            return
        dlg = SyncPairDialog(
            self.drive, existing_pair=self._pairs[row], parent=self
        )
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_pair is not None:
            self._pairs[row] = dlg.result_pair
            self._refresh_pairs_table()
            self._persist_current_state()

    def _remove_selected_pair(self) -> None:
        row = self.pairs_table.currentRow()
        if row < 0 or row >= len(self._pairs):
            return
        pair = self._pairs[row]
        ans = QMessageBox.question(
            self,
            "Remove Folder Sync Connection",
            f"Stop syncing '{pair.local_path}' with Google Drive '{pair.remote_path}'?\n\n"
            "(Your local and remote files will NOT be deleted.)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            removed = self._pairs.pop(row)
            self.db.clear_pair_cache(removed.pair_id)
            self._refresh_pairs_table()
            self._persist_current_state()

    def _open_selected_local_folder(self) -> None:
        row = self.pairs_table.currentRow()
        if 0 <= row < len(self._pairs):
            open_local_folder(Path(self._pairs[row].local_path).expanduser())
        elif self._pairs:
            open_local_folder(Path(self._pairs[0].local_path).expanduser())

    def _sync_selected_pair_now(self) -> None:
        row = self.pairs_table.currentRow()
        if 0 <= row < len(self._pairs):
            self._persist_current_state()
            self.sync_pair_now_requested.emit(self._pairs[row].pair_id)

    def _persist_current_state(self) -> Dict[str, Any]:
        proxy_mode = str(self.proxy_mode_combo.currentData() or "system")
        conflict_strategy = str(self.conflict_strategy_combo.currentData() or "keep_both")
        autostart = bool(self.autostart_check.isChecked())
        set_autostart_enabled(autostart)

        updated: Dict[str, Any] = {
            "sync_pairs": [p.to_dict() for p in self._pairs],
            "poll_interval_seconds": int(self.poll_spin.value()),
            "debounce_seconds": float(self.debounce_spin.value()),
            "notifications_enabled": bool(self.notifications_check.isChecked()),
            "rate_limit_upload_kbps": int(self.upload_limit_spin.value()),
            "rate_limit_download_kbps": int(self.download_limit_spin.value()),
            "proxy_mode": proxy_mode,
            "proxy_host": self.proxy_host_edit.text().strip(),
            "proxy_port": int(self.proxy_port_spin.value()),
            "proxy_user": self.proxy_user_edit.text().strip(),
            "proxy_pass": self.proxy_pass_edit.text().strip(),
            "max_file_size_mb": int(self.max_file_size_spin.value()),
            "conflict_strategy": conflict_strategy,
            "autostart": autostart,
            "start_minimized": bool(self.start_minimized_check.isChecked()),
        }
        if self._pairs:
            updated["sync_folder"] = self._pairs[0].local_path
            updated["remote_folder_name"] = self._pairs[0].remote_path.strip("/")

        self.drive.apply_network_settings(
            proxy_mode=proxy_mode,
            proxy_host=updated["proxy_host"],
            proxy_port=updated["proxy_port"],
            proxy_user=updated["proxy_user"],
            proxy_pass=updated["proxy_pass"],
            rate_limit_upload_kbps=updated["rate_limit_upload_kbps"],
            rate_limit_download_kbps=updated["rate_limit_download_kbps"],
        )

        self.db.save_settings(updated)
        self.settings_saved.emit(updated)
        return updated

    def _on_save_clicked(self) -> None:
        self._persist_current_state()
        self.hide()

    def closeEvent(self, event: QCloseEvent) -> None:
        """Override `closeEvent` for 'Close to Tray' window behavior under Wayland."""
        if self._allow_quit_cb is not None and self._allow_quit_cb():
            event.accept()
            return

        event.ignore()
        self.hide()
        if not self._notified_close_to_tray:
            self._notified_close_to_tray = True
            self.closed_to_tray.emit()
