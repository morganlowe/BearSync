"""Nextcloud/Celeste-style Setup Wizard: One-Click Browser Sign-In -> Multi-Folder Sync Connections."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from src.db import SyncDatabase, SyncPair
from src.drive_api import DriveAPIError, DriveClient
from src.ui.folder_pair_dialog import SyncPairDialog

logger = logging.getLogger(__name__)


class _OAuthWorkerThread(QThread):
    """Executes the loopback OAuth2 browser handshake and fetches Google account info."""

    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, drive: DriveClient, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.drive = drive

    def run(self) -> None:
        try:
            self.drive.authenticate(interactive=True, open_browser=True)
            info = self.drive.get_account_info()
            self.succeeded.emit(info)
        except Exception as exc:
            self.failed.emit(str(exc))


class SetupWizard(QWizard):
    """Nextcloud/Celeste-style first-run onboarding:

    Page 1: One-click 'Sign in with Google' via default web browser
    Page 2: Configure Folder Sync Connections (map any local folders to any Google Drive subfolders)
    """

    setup_completed = pyqtSignal(dict)

    def __init__(
        self,
        db: SyncDatabase,
        drive: DriveClient,
        parent: Optional[QWidget] = None,
        allow_quit_cb: Optional[Callable[[], bool]] = None,
    ) -> None:
        super().__init__(parent)
        self.db = db
        self.drive = drive
        self._allow_quit_cb = allow_quit_cb
        self._oauth_thread: Optional[_OAuthWorkerThread] = None
        self._pairs: List[SyncPair] = self.db.get_sync_pairs()
        self._account_email = str(self.db.get_settings().get("account_email") or "")
        self._account_name = str(self.db.get_settings().get("account_name") or "")

        self.setWindowTitle("BearSync — Account & Folder Sync Setup")
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.setMinimumSize(680, 460)

        self._account_page = self._create_account_page()
        self._folders_page = self._create_folders_page()

        self.addPage(self._account_page)
        self.addPage(self._folders_page)

        self.accepted.connect(self._on_wizard_accepted)

    def _create_account_page(self) -> QWizardPage:
        page = QWizardPage(self)
        page.setTitle("Step 1: Sign In to Google Drive")
        page.setSubTitle(
            "Click 'Sign in with Google' below to open your web browser and link your Google Drive account."
        )

        layout = QVBoxLayout(page)
        layout.setSpacing(16)

        signin_group = QGroupBox("Google Account Web Sign-In", page)
        signin_layout = QVBoxLayout(signin_group)
        signin_layout.setSpacing(12)

        info_label = QLabel(
            "BearSync will open Google's sign-in page in your default browser. "
            "Once you click 'Allow', BearSync automatically receives your authorization token.",
            signin_group,
        )
        info_label.setWordWrap(True)
        signin_layout.addWidget(info_label)

        self.auth_btn = QPushButton(
            QIcon.fromTheme("internet-web-browser"),
            "Sign in with Google (Open Web Browser)",
            signin_group,
        )
        self.auth_btn.setMinimumHeight(44)
        self.auth_btn.clicked.connect(self._start_oauth_flow)
        signin_layout.addWidget(self.auth_btn)

        status_text = "Status: Not signed in yet"
        if self.drive.has_valid_token():
            if self._account_email:
                status_text = (
                    f"Status: Connected as {self._account_name} <{self._account_email}> ✓"
                )
            else:
                status_text = "Status: Connected to Google Drive ✓"

        self.auth_status_label = QLabel(status_text, signin_group)
        self.auth_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        signin_layout.addWidget(self.auth_status_label)

        layout.addWidget(signin_group)

        # Optional advanced custom OAuth credentials (collapsed by default)
        self.advanced_check = QCheckBox(
            "Advanced: Use custom Google Cloud OAuth2 client_secret.json (optional)",
            page,
        )
        layout.addWidget(self.advanced_check)

        self.advanced_container = QWidget(page)
        adv_layout = QVBoxLayout(self.advanced_container)
        adv_layout.setContentsMargins(0, 0, 0, 0)

        top_row = QHBoxLayout()
        load_btn = QPushButton(
            QIcon.fromTheme("document-open"),
            "Load client_secret.json File...",
            self.advanced_container,
        )
        load_btn.clicked.connect(self._load_credentials_file)
        clear_custom_btn = QPushButton(
            "Use Built-in Default Client", self.advanced_container
        )
        clear_custom_btn.clicked.connect(self._use_default_client)
        top_row.addWidget(load_btn)
        top_row.addWidget(clear_custom_btn)
        top_row.addStretch(1)
        adv_layout.addLayout(top_row)

        self.credentials_text = QPlainTextEdit(self.advanced_container)
        self.credentials_text.setPlaceholderText(
            'Optional: Paste custom {"installed": {"client_id": "...", "client_secret": "..."}} JSON here'
        )
        if self.drive.credentials_path.is_file():
            try:
                self.credentials_text.setPlainText(
                    self.drive.credentials_path.read_text(encoding="utf-8")
                )
            except OSError:
                pass
        adv_layout.addWidget(self.credentials_text)

        self.advanced_container.setVisible(False)
        self.advanced_check.toggled.connect(self.advanced_container.setVisible)
        layout.addWidget(self.advanced_container)

        layout.addStretch(1)
        return page

    def _create_folders_page(self) -> QWizardPage:
        page = QWizardPage(self)
        page.setTitle("Step 2: Configure Folder Sync Connections")
        page.setSubTitle(
            "Add one or more Local ↔ Google Drive folder pairs. You can map any local directory "
            "to any Google Drive folder or subfolder (e.g. /Backups/Project)."
        )

        layout = QVBoxLayout(page)

        self.pairs_table = QTableWidget(0, 3, page)
        self.pairs_table.setHorizontalHeaderLabels(
            ["Local Directory", "Google Drive Folder", "Versioning"]
        )
        header = self.pairs_table.horizontalHeader()
        if header is not None:
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.pairs_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.pairs_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        layout.addWidget(self.pairs_table, stretch=1)

        btn_row = QHBoxLayout()
        add_btn = QPushButton(
            QIcon.fromTheme("list-add"), "+ Add Folder Sync Connection...", page
        )
        add_btn.clicked.connect(self._add_sync_pair)
        edit_btn = QPushButton(
            QIcon.fromTheme("document-edit"), "Edit Selected...", page
        )
        edit_btn.clicked.connect(self._edit_selected_pair)
        remove_btn = QPushButton(
            QIcon.fromTheme("list-remove"), "Remove Selected", page
        )
        remove_btn.clicked.connect(self._remove_selected_pair)
        btn_row.addWidget(add_btn)
        btn_row.addWidget(edit_btn)
        btn_row.addWidget(remove_btn)
        btn_row.addStretch(1)
        layout.addLayout(btn_row)

        settings = self.db.get_settings()
        sched_form = QFormLayout()
        self.poll_spin = QSpinBox(page)
        self.poll_spin.setRange(15, 86400)
        self.poll_spin.setValue(int(settings.get("poll_interval_seconds", 60)))
        self.poll_spin.setSuffix(" seconds")
        sched_form.addRow("Remote Poll Interval:", self.poll_spin)
        layout.addLayout(sched_form)

        self._refresh_pairs_table()
        return page

    def _refresh_pairs_table(self) -> None:
        self.pairs_table.setRowCount(0)
        for pair in self._pairs:
            row = self.pairs_table.rowCount()
            self.pairs_table.insertRow(row)
            self.pairs_table.setItem(row, 0, QTableWidgetItem(pair.local_path))
            self.pairs_table.setItem(row, 1, QTableWidgetItem(pair.remote_path))
            ver_text = (
                f"ON ({'.versions/' if pair.versioning_mode == 'versions_subdir' else 'inline'})"
                if pair.versioning_enabled
                else "OFF"
            )
            self.pairs_table.setItem(row, 2, QTableWidgetItem(ver_text))

    def _add_sync_pair(self) -> None:
        dlg = SyncPairDialog(self.drive, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_pair is not None:
            self._pairs.append(dlg.result_pair)
            self._refresh_pairs_table()

    def _edit_selected_pair(self) -> None:
        row = self.pairs_table.currentRow()
        if row < 0 or row >= len(self._pairs):
            return
        dlg = SyncPairDialog(self.drive, existing_pair=self._pairs[row], parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_pair is not None:
            self._pairs[row] = dlg.result_pair
            self._refresh_pairs_table()

    def _remove_selected_pair(self) -> None:
        row = self.pairs_table.currentRow()
        if row < 0 or row >= len(self._pairs):
            return
        self._pairs.pop(row)
        self._refresh_pairs_table()

    def _load_credentials_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select OAuth2 client_secret.json",
            str(Path.home()),
            "JSON Files (*.json);;All Files (*)",
        )
        if file_path:
            try:
                content = Path(file_path).read_text(encoding="utf-8")
                self.credentials_text.setPlainText(content)
            except OSError as exc:
                QMessageBox.warning(self, "Read Error", f"Could not read file: {exc}")

    def _use_default_client(self) -> None:
        self.credentials_text.clear()
        if self.drive.credentials_path.exists():
            self.drive.credentials_path.unlink(missing_ok=True)
        self.advanced_check.setChecked(False)

    def _start_oauth_flow(self) -> None:
        if self.advanced_check.isChecked():
            raw_json = self.credentials_text.toPlainText().strip()
            if raw_json:
                try:
                    self.drive.save_client_credentials_json(raw_json)
                except DriveAPIError as exc:
                    QMessageBox.critical(self, "Invalid Credentials JSON", str(exc))
                    return
        else:
            # Ensure built-in one-click OAuth client is used
            if self.drive.credentials_path.exists() and not self.credentials_text.toPlainText().strip():
                self.drive.credentials_path.unlink(missing_ok=True)

        self.auth_btn.setEnabled(False)
        self.auth_status_label.setText(
            "Status: Waiting for Google sign-in in your web browser..."
        )

        self._oauth_thread = _OAuthWorkerThread(self.drive, self)
        self._oauth_thread.succeeded.connect(self._on_oauth_succeeded)
        self._oauth_thread.failed.connect(self._on_oauth_failed)
        self._oauth_thread.start()

    def _on_oauth_succeeded(self, info: Dict[str, object]) -> None:
        self.auth_btn.setEnabled(True)
        self._account_email = str(info.get("email") or "")
        self._account_name = str(info.get("display_name") or "")
        if self._account_email:
            self.auth_status_label.setText(
                f"Status: Connected as {self._account_name} <{self._account_email}> ✓"
            )
        else:
            self.auth_status_label.setText("Status: Connected to Google Drive ✓")

    def _on_oauth_failed(self, error_msg: str) -> None:
        self.auth_btn.setEnabled(True)
        self.auth_status_label.setText("Status: Authentication failed")
        QMessageBox.critical(
            self,
            "Google Sign-In Failed",
            f"Could not complete Google Drive sign-in:\n{error_msg}",
        )

    def _on_wizard_accepted(self) -> None:
        settings = {
            "sync_pairs": [p.to_dict() for p in self._pairs],
            "account_email": self._account_email,
            "account_name": self._account_name,
            "poll_interval_seconds": int(self.poll_spin.value()),
            "setup_completed": True,
        }
        if self._pairs:
            settings["sync_folder"] = self._pairs[0].local_path
            settings["remote_folder_name"] = self._pairs[0].remote_path.strip("/")

        self.db.save_settings(settings)
        self.setup_completed.emit(settings)

    def closeEvent(self, event: QCloseEvent) -> None:
        """Close to system tray unless the application is shutting down."""
        if self._allow_quit_cb is not None and self._allow_quit_cb():
            event.accept()
            return
        event.ignore()
        self.hide()
