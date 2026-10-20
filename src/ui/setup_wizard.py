"""Nextcloud/Celeste-style Setup Wizard: One-Click Browser Sign-In -> Multi-Folder Sync Connections."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, Dict, List, Optional

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QCloseEvent, QDesktopServices, QIcon
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
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
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


class OAuthInstructionsDialog(QDialog):
    """Step-by-step interactive visual guide on creating Google Cloud OAuth credentials."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("How to Get Google Drive OAuth Credentials — BearSync")
        self.setMinimumSize(660, 560)

        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        header = QLabel(
            "<h3>Google Drive API Credentials Setup Guide</h3>"
            "<p>BearSync is an open, private sync client that connects directly from your device "
            "to Google Drive without any intermediary servers. Follow these quick steps to generate your "
            "personal Google API keys for free:</p>",
            self,
        )
        header.setWordWrap(True)
        layout.addWidget(header)

        steps_scroll = QScrollArea(self)
        steps_scroll.setWidgetResizable(True)
        container = QWidget(steps_scroll)
        container_layout = QVBoxLayout(container)

        steps_text = QLabel(
            "<ol style='line-height: 1.6; font-size: 13px;'>"
            "<li><b>Open Google Cloud Console:</b> Go to <a href='https://console.cloud.google.com/'>console.cloud.google.com</a> and sign in with your Google account.</li>"
            "<li><b>Create a Project:</b> Click the project dropdown at the top and click <b>New Project</b> (e.g. name it <i>BearSync</i>).</li>"
            "<li><b>Enable Google Drive API:</b> Go to <b>APIs & Services &rarr; Library</b>, search for <b>Google Drive API</b>, and click <b>Enable</b>.</li>"
            "<li><b>Configure OAuth Consent Screen:</b>"
            "  <ul>"
            "    <li>Go to <b>APIs & Services &rarr; OAuth consent screen</b>.</li>"
            "    <li>Choose <b>External</b> and click <i>Create</i>.</li>"
            "    <li>Fill in <b>App name</b> (e.g. <code>BearSync</code>) and your user support email.</li>"
            "    <li>Under <b>Test Users</b>, click <b>+ Add Users</b> and enter your Google email address.</li>"
            "    <li>Save and continue to the end.</li>"
            "  </ul>"
            "</li>"
            "<li><b>Create OAuth Client ID:</b>"
            "  <ul>"
            "    <li>Go to <b>APIs & Services &rarr; Credentials</b> &rarr; click <b>+ Create Credentials &rarr; OAuth client ID</b>.</li>"
            "    <li>Under <b>Application type</b>, select <b>Desktop app</b>.</li>"
            "    <li>Name it <code>BearSync Desktop</code> and click <b>Create</b>.</li>"
            "  </ul>"
            "</li>"
            "<li><b>Copy Keys or Download JSON:</b> Copy the generated <b>Client ID</b> and <b>Client Secret</b>, or click <b>Download JSON</b>. Enter them into BearSync!</li>"
            "</ol>",
            container,
        )
        steps_text.setWordWrap(True)
        steps_text.setOpenExternalLinks(True)
        container_layout.addWidget(steps_text)
        steps_scroll.setWidget(container)
        layout.addWidget(steps_scroll, stretch=1)

        btn_row = QHBoxLayout()
        console_btn = QPushButton(
            QIcon.fromTheme("internet-web-browser"),
            "Open Google Cloud Console in Browser",
            self,
        )
        console_btn.clicked.connect(self._open_console)
        btn_row.addWidget(console_btn)
        btn_row.addStretch(1)

        close_btn = QPushButton("Close", self)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    def _open_console(self) -> None:
        QDesktopServices.openUrl(QUrl("https://console.cloud.google.com/apis/credentials"))


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

    Page 1: Google OAuth credentials setup & one-click 'Sign in with Google' via default web browser
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
        self.setMinimumSize(720, 520)

        self._account_page = self._create_account_page()
        self._folders_page = self._create_folders_page()

        self.addPage(self._account_page)
        self.addPage(self._folders_page)

        self.accepted.connect(self._on_wizard_accepted)

    def _create_account_page(self) -> QWizardPage:
        page = QWizardPage(self)
        page.setTitle("Step 1: Link Your Google Drive Account")
        page.setSubTitle(
            "BearSync connects directly to Google Drive via your own private Google Cloud credentials."
        )

        layout = QVBoxLayout(page)
        layout.setSpacing(12)

        # --- Section 1: Sign-in action ---
        signin_group = QGroupBox("Google Account Connection", page)
        signin_layout = QVBoxLayout(signin_group)
        signin_layout.setSpacing(10)

        status_text = "Status: Not signed in yet"
        if self.drive.has_valid_token():
            if self._account_email:
                status_text = (
                    f"Connected as: {self._account_name} <{self._account_email}> ✓"
                )
            else:
                status_text = "Status: Connected to Google Drive ✓"

        self.auth_status_label = QLabel(status_text, signin_group)
        self.auth_status_label.setStyleSheet("font-weight: bold;")
        signin_layout.addWidget(self.auth_status_label)

        btn_row = QHBoxLayout()
        self.auth_btn = QPushButton(
            QIcon.fromTheme("internet-web-browser"),
            "Sign in with Google (Open Web Browser)",
            signin_group,
        )
        self.auth_btn.setMinimumHeight(40)
        self.auth_btn.clicked.connect(self._start_oauth_flow)
        btn_row.addWidget(self.auth_btn, stretch=2)

        self.disconnect_btn = QPushButton(
            QIcon.fromTheme("edit-delete"), "Disconnect", signin_group
        )
        self.disconnect_btn.clicked.connect(self._disconnect_account)
        self.disconnect_btn.setEnabled(self.drive.has_valid_token())
        btn_row.addWidget(self.disconnect_btn, stretch=1)
        signin_layout.addLayout(btn_row)

        layout.addWidget(signin_group)

        # --- Section 2: OAuth Credentials Management ---
        creds_group = QGroupBox("Google Cloud API Credentials", page)
        creds_layout = QVBoxLayout(creds_group)
        creds_layout.setSpacing(8)

        info_row = QHBoxLayout()
        self.creds_badge_label = QLabel(creds_group)
        self._refresh_creds_badge()
        info_row.addWidget(self.creds_badge_label, stretch=1)

        guide_btn = QPushButton(
            QIcon.fromTheme("help-about"), "📘 Setup Instructions...", creds_group
        )
        guide_btn.clicked.connect(self._show_instructions_dialog)
        info_row.addWidget(guide_btn)
        creds_layout.addLayout(info_row)

        form_layout = QFormLayout()
        self.client_id_edit = QLineEdit(creds_group)
        self.client_id_edit.setPlaceholderText("e.g. 123456789-abcdef.apps.googleusercontent.com")
        form_layout.addRow("Client ID:", self.client_id_edit)

        secret_row = QHBoxLayout()
        self.client_secret_edit = QLineEdit(creds_group)
        self.client_secret_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.client_secret_edit.setPlaceholderText("e.g. GOCSPX-xxxxxxxxxxxxxxxxxxxx")
        secret_row.addWidget(self.client_secret_edit)

        self.show_secret_check = QCheckBox("Show", creds_group)
        self.show_secret_check.toggled.connect(self._toggle_secret_visibility)
        secret_row.addWidget(self.show_secret_check)
        form_layout.addRow("Client Secret:", secret_row)

        creds_layout.addLayout(form_layout)

        # Populate if already available
        creds_info = self.drive.get_client_credentials_info()
        if creds_info.get("client_id"):
            self.client_id_edit.setText(creds_info["client_id"])
        if creds_info.get("client_secret"):
            self.client_secret_edit.setText(creds_info["client_secret"])

        actions_row = QHBoxLayout()
        save_btn = QPushButton(
            QIcon.fromTheme("document-save"), "Save Credentials", creds_group
        )
        save_btn.clicked.connect(self._save_credentials_inputs)
        actions_row.addWidget(save_btn)

        load_btn = QPushButton(
            QIcon.fromTheme("document-open"), "Load client_secret.json...", creds_group
        )
        load_btn.clicked.connect(self._load_credentials_file)
        actions_row.addWidget(load_btn)

        clear_btn = QPushButton(
            QIcon.fromTheme("edit-clear"), "Clear", creds_group
        )
        clear_btn.clicked.connect(self._clear_credentials)
        actions_row.addWidget(clear_btn)

        actions_row.addStretch(1)
        creds_layout.addLayout(actions_row)

        layout.addWidget(creds_group)
        layout.addStretch(1)
        return page

    def _refresh_creds_badge(self) -> None:
        info = self.drive.get_client_credentials_info()
        if info.get("client_id"):
            src = "custom file (~/.config/gdrive-sync/credentials.json)" if info.get("source") == "file" else "environment variable"
            self.creds_badge_label.setText(
                f"<span style='color: #27ae60; font-weight: bold;'>✓ Credentials active</span> ({src})"
            )
        else:
            self.creds_badge_label.setText(
                "<span style='color: #e67e22; font-weight: bold;'>⚠️ Credentials required</span> — enter below or click Setup Instructions"
            )

    def _toggle_secret_visibility(self, visible: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.client_secret_edit.setEchoMode(mode)

    def _show_instructions_dialog(self) -> None:
        dlg = OAuthInstructionsDialog(parent=self)
        dlg.exec()

    def _save_credentials_inputs(self) -> bool:
        cid = self.client_id_edit.text().strip()
        sec = self.client_secret_edit.text().strip()
        if not cid or not sec:
            QMessageBox.warning(
                self,
                "Missing Fields",
                "Please enter both a Google Client ID and Client Secret.",
            )
            return False
        try:
            self.drive.save_client_credentials(cid, sec)
            self._refresh_creds_badge()
            QMessageBox.information(
                self,
                "Credentials Saved",
                "Google OAuth credentials saved successfully to ~/.config/gdrive-sync/credentials.json.\n\n"
                "You can now click 'Sign in with Google'.",
            )
            return True
        except Exception as exc:
            QMessageBox.critical(self, "Save Error", f"Could not save credentials: {exc}")
            return False

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
                self.drive.save_client_credentials_json(content)
                creds_info = self.drive.get_client_credentials_info()
                self.client_id_edit.setText(creds_info.get("client_id", ""))
                self.client_secret_edit.setText(creds_info.get("client_secret", ""))
                self._refresh_creds_badge()
                QMessageBox.information(
                    self,
                    "Loaded",
                    "OAuth credentials loaded and saved to ~/.config/gdrive-sync/credentials.json successfully!",
                )
            except Exception as exc:
                QMessageBox.warning(self, "Load Error", f"Could not load credentials: {exc}")

    def _clear_credentials(self) -> None:
        if self.drive.credentials_path.is_file():
            self.drive.credentials_path.unlink(missing_ok=True)
        self.client_id_edit.clear()
        self.client_secret_edit.clear()
        self._refresh_creds_badge()

    def _disconnect_account(self) -> None:
        self.drive.disconnect_account()
        self._account_email = ""
        self._account_name = ""
        self.auth_status_label.setText("Status: Not signed in yet")
        self.disconnect_btn.setEnabled(False)
        self.db.save_settings({"account_email": "", "account_name": ""})
        QMessageBox.information(
            self, "Disconnected", "Google Drive account disconnected successfully."
        )

    def _start_oauth_flow(self) -> None:
        # Check if credentials exist
        if not self.drive.has_client_credentials():
            # If user has entered values in the text fields, offer to save them
            cid = self.client_id_edit.text().strip()
            sec = self.client_secret_edit.text().strip()
            if cid and sec:
                if not self._save_credentials_inputs():
                    return
            else:
                reply = QMessageBox.question(
                    self,
                    "Credentials Required",
                    "Before signing in with Google, you need to configure your Google Cloud API keys.\n\n"
                    "Would you like to open the step-by-step setup guide now?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._show_instructions_dialog()
                return

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
        self.disconnect_btn.setEnabled(True)
        self._account_email = str(info.get("email") or "")
        self._account_name = str(info.get("display_name") or "")
        if self._account_email:
            self.auth_status_label.setText(
                f"Connected as: {self._account_name} <{self._account_email}> ✓"
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
