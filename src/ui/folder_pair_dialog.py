"""Nextcloud-style Folder Sync Connection dialog and interactive Google Drive folder browser/creator."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.db import SyncPair
from src.drive_api import DriveClient

logger = logging.getLogger(__name__)

ROLE_FOLDER_ID = Qt.ItemDataRole.UserRole
ROLE_REMOTE_PATH = Qt.ItemDataRole.UserRole + 1
ROLE_LOADED = Qt.ItemDataRole.UserRole + 2


class RemoteFolderPickerDialog(QDialog):
    """Interactive Google Drive folder tree browser with 'New Folder / Subfolder' creation."""

    def __init__(
        self,
        drive: DriveClient,
        initial_path: str = "/BearSync",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.drive = drive
        self.selected_remote_path = initial_path
        self.selected_folder_id = ""

        self.setWindowTitle("BearSync — Choose or Create Google Drive Folder")
        self.setMinimumSize(520, 420)
        self._build_ui(initial_path)
        self._populate_root()

    def _build_ui(self, initial_path: str) -> None:
        layout = QVBoxLayout(self)

        info_label = QLabel(
            "Select an existing folder on Google Drive, or click 'New Folder / Subfolder...' "
            "to create a directory structure (e.g. /Backups/Work).",
            self,
        )
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderLabels(["Google Drive Folders", "Remote Path"])
        self.tree.setColumnWidth(0, 260)
        self.tree.itemExpanded.connect(self._on_item_expanded)
        self.tree.currentItemChanged.connect(self._on_current_item_changed)
        layout.addWidget(self.tree, stretch=1)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("Target Drive Path:", self))
        self.path_edit = QLineEdit(initial_path, self)
        self.path_edit.setPlaceholderText("/Backups/Work")
        path_row.addWidget(self.path_edit, stretch=1)
        layout.addLayout(path_row)

        action_row = QHBoxLayout()
        new_folder_btn = QPushButton(
            QIcon.fromTheme("folder-new"), "+ New Folder / Subfolder...", self
        )
        new_folder_btn.clicked.connect(self._create_new_subfolder)
        refresh_btn = QPushButton(QIcon.fromTheme("view-refresh"), "Refresh", self)
        refresh_btn.clicked.connect(self._populate_root)
        action_row.addWidget(new_folder_btn)
        action_row.addWidget(refresh_btn)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_root(self) -> None:
        self.tree.clear()
        root_item = QTreeWidgetItem(self.tree, ["My Drive (/)", "/"])
        root_item.setIcon(0, QIcon.fromTheme("folder-cloud"))
        root_item.setData(0, ROLE_FOLDER_ID, "root")
        root_item.setData(0, ROLE_REMOTE_PATH, "/")
        root_item.setData(0, ROLE_LOADED, False)
        QTreeWidgetItem(root_item, ["Loading..."])
        root_item.setExpanded(True)
        self.tree.setCurrentItem(root_item)

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if bool(item.data(0, ROLE_LOADED)):
            return
        folder_id = str(item.data(0, ROLE_FOLDER_ID) or "root")
        parent_path = str(item.data(0, ROLE_REMOTE_PATH) or "/").rstrip("/")

        item.takeChildren()
        try:
            subfolders = self.drive.list_subfolders(folder_id)
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Google Drive Error",
                f"Could not list folders on Google Drive:\n{exc}",
            )
            return

        item.setData(0, ROLE_LOADED, True)
        for sub in subfolders:
            sub_path = f"{parent_path}/{sub.name}"
            child = QTreeWidgetItem(item, [sub.name, sub_path])
            child.setIcon(0, QIcon.fromTheme("folder"))
            child.setData(0, ROLE_FOLDER_ID, sub.folder_id)
            child.setData(0, ROLE_REMOTE_PATH, sub_path)
            child.setData(0, ROLE_LOADED, False)
            QTreeWidgetItem(child, ["Loading..."])

    def _on_current_item_changed(
        self, current: Optional[QTreeWidgetItem], _previous: Optional[QTreeWidgetItem]
    ) -> None:
        if current is None:
            return
        remote_path = str(current.data(0, ROLE_REMOTE_PATH) or "")
        if remote_path:
            self.path_edit.setText(remote_path)
            self.selected_folder_id = str(current.data(0, ROLE_FOLDER_ID) or "")

    def _create_new_subfolder(self) -> None:
        current = self.tree.currentItem()
        base_path = "/"
        if current is not None:
            base_path = str(current.data(0, ROLE_REMOTE_PATH) or "/")

        text, ok = QInputDialog.getText(
            self,
            "Create New Google Drive Folder / Subfolder",
            f"Enter new folder or nested path inside '{base_path}'\n"
            "(e.g. 'Backups/Project'):",
        )
        if not ok or not text.strip():
            return

        rel_new = text.strip().replace("\\", "/").strip("/")
        full_new_path = f"{base_path.rstrip('/')}/{rel_new}"

        try:
            folder_id = self.drive.ensure_remote_path(full_new_path)
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Create Folder Failed",
                f"Could not create '{full_new_path}' on Google Drive:\n{exc}",
            )
            return

        self.path_edit.setText(full_new_path)
        self.selected_remote_path = full_new_path
        self.selected_folder_id = folder_id
        self._populate_root()

    def _on_accept(self) -> None:
        typed_path = "/" + self.path_edit.text().strip().replace("\\", "/").strip("/")
        if typed_path == "/":
            QMessageBox.warning(
                self,
                "Select a Folder",
                "Please enter or select a folder path inside Google Drive (e.g. /Backups/Work).",
            )
            return

        self.selected_remote_path = typed_path
        if self.drive.has_valid_token():
            try:
                self.selected_folder_id = self.drive.ensure_remote_path(typed_path)
            except Exception as exc:
                logger.warning("Will create remote folder on next sync pass: %s", exc)
                self.selected_folder_id = ""
        self.accept()


class SyncPairDialog(QDialog):
    """Nextcloud-style dialog to add or edit a Local <-> Google Drive folder sync connection."""

    def __init__(
        self,
        drive: DriveClient,
        existing_pair: Optional[SyncPair] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.drive = drive
        self._existing_pair = existing_pair
        self.result_pair: Optional[SyncPair] = None
        self._resolved_remote_id = existing_pair.remote_folder_id if existing_pair else ""

        self.setWindowTitle(
            "Edit Folder Sync Connection"
            if existing_pair
            else "Add Folder Sync Connection"
        )
        self.setMinimumWidth(580)
        self._build_ui()
        if existing_pair:
            self._load_pair(existing_pair)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # --- 1. Local Directory Group ---
        local_group = QGroupBox("1. Local Directory to Synchronize", self)
        local_form = QFormLayout(local_group)

        local_row = QHBoxLayout()
        self.local_path_edit = QLineEdit(self)
        self.local_path_edit.setPlaceholderText("/home/user/Documents/Project")
        browse_local_btn = QPushButton(
            QIcon.fromTheme("folder-open"), "Browse Local...", self
        )
        browse_local_btn.clicked.connect(self._browse_local_dir)
        local_row.addWidget(self.local_path_edit, stretch=1)
        local_row.addWidget(browse_local_btn)
        local_form.addRow("Local Path:", local_row)

        layout.addWidget(local_group)

        # --- 2. Remote Google Drive Folder Group ---
        remote_group = QGroupBox("2. Target Google Drive Folder / Subfolder", self)
        remote_layout = QVBoxLayout(remote_group)

        remote_hint = QLabel(
            "Type any multi-level Google Drive path (missing folders & subfolders are created automatically), "
            "or browse and create folders interactively:",
            self,
        )
        remote_hint.setWordWrap(True)
        remote_layout.addWidget(remote_hint)

        remote_row = QHBoxLayout()
        self.remote_path_edit = QLineEdit(self)
        self.remote_path_edit.setPlaceholderText("/Backups/Project")
        self.remote_path_edit.textEdited.connect(self._on_remote_text_edited)
        browse_remote_btn = QPushButton(
            QIcon.fromTheme("folder-cloud"), "Browse / Create in Google Drive...", self
        )
        browse_remote_btn.clicked.connect(self._open_remote_picker)
        remote_row.addWidget(self.remote_path_edit, stretch=1)
        remote_row.addWidget(browse_remote_btn)
        remote_layout.addLayout(remote_row)

        layout.addWidget(remote_group)

        # --- 3. Optional Timestamp Versioning ---
        ver_group = QGroupBox("3. Version Control (Optional)", self)
        ver_layout = QVBoxLayout(ver_group)

        mode_form = QFormLayout()
        self.version_mode_combo = QComboBox(self)
        self.version_mode_combo.addItem(
            "Disabled — Do not keep old file versions", "disabled"
        )
        self.version_mode_combo.addItem(
            "Enabled — Archive into .versions/ subdirectory", "versions_subdir"
        )
        self.version_mode_combo.addItem(
            "Enabled — Append _YYYYMMDD_HHMMSS alongside file", "inline_suffix"
        )
        self.version_mode_combo.currentIndexChanged.connect(self._on_version_mode_changed)
        mode_form.addRow("Version Control:", self.version_mode_combo)
        ver_layout.addLayout(mode_form)

        # Container for versioning options, hidden when Version Control is Disabled
        self.ver_options_widget = QWidget(self)
        ver_opts_form = QFormLayout(self.ver_options_widget)
        ver_opts_form.setContentsMargins(0, 4, 0, 0)

        self.max_versions_spin = QSpinBox(self.ver_options_widget)
        self.max_versions_spin.setRange(1, 500)
        self.max_versions_spin.setValue(10)
        self.max_versions_spin.setSuffix(" versions per file")
        ver_opts_form.addRow("Retention Limit:", self.max_versions_spin)

        self.subpaths_edit = QLineEdit("*", self.ver_options_widget)
        self.subpaths_edit.setPlaceholderText("* (entire folder) or subfolder1, subfolder2")
        ver_opts_form.addRow("Monitored Subpaths:", self.subpaths_edit)

        self.ver_options_widget.setVisible(False)
        ver_layout.addWidget(self.ver_options_widget)

        layout.addWidget(ver_group)

        # --- Dialog Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            Qt.Orientation.Horizontal,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_version_mode_changed(self, _index: int) -> None:
        mode = str(self.version_mode_combo.currentData() or "disabled")
        self.ver_options_widget.setVisible(mode != "disabled")

    def _load_pair(self, pair: SyncPair) -> None:
        self.local_path_edit.setText(pair.local_path)
        self.remote_path_edit.setText(pair.remote_path)
        if not pair.versioning_enabled:
            self.version_mode_combo.setCurrentIndex(0)
        else:
            idx = self.version_mode_combo.findData(pair.versioning_mode)
            self.version_mode_combo.setCurrentIndex(idx if idx >= 0 else 1)
        self.max_versions_spin.setValue(pair.max_versions_per_file)
        self.subpaths_edit.setText(", ".join(pair.versioning_paths or ["*"]))

    def _browse_local_dir(self) -> None:
        start_dir = self.local_path_edit.text().strip() or str(Path.home())
        if not Path(start_dir).exists():
            start_dir = str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Select Local Folder to Synchronize",
            start_dir,
            QFileDialog.Option.ShowDirsOnly,
        )
        if selected:
            self.local_path_edit.setText(selected)
            if not self.remote_path_edit.text().strip():
                folder_name = Path(selected).name
                self.remote_path_edit.setText(f"/{folder_name}")

    def _on_remote_text_edited(self, _text: str) -> None:
        self._resolved_remote_id = ""

    def _open_remote_picker(self) -> None:
        if not self.drive.has_valid_token():
            QMessageBox.information(
                self,
                "Google Account Not Connected Yet",
                "Connect your Google Drive account first to browse existing remote folders, "
                "or simply type the target path (e.g. /Backups/Project) and BearSync "
                "will create the folders automatically when syncing.",
            )
            return

        initial = self.remote_path_edit.text().strip() or "/BearSync"
        picker = RemoteFolderPickerDialog(self.drive, initial_path=initial, parent=self)
        if picker.exec() == QDialog.DialogCode.Accepted:
            self.remote_path_edit.setText(picker.selected_remote_path)
            self._resolved_remote_id = picker.selected_folder_id

    def _on_accept(self) -> None:
        raw_local = self.local_path_edit.text().strip()
        raw_remote = self.remote_path_edit.text().strip()
        if not raw_local:
            QMessageBox.warning(
                self, "Missing Local Path", "Please choose a local directory to sync."
            )
            return
        if not raw_remote:
            QMessageBox.warning(
                self,
                "Missing Google Drive Path",
                "Please specify a target Google Drive folder path (e.g. /Backups/Project).",
            )
            return

        local_dir = Path(raw_local).expanduser()
        try:
            local_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Invalid Local Directory",
                f"Could not access or create local directory:\n{exc}",
            )
            return

        norm_remote = "/" + raw_remote.replace("\\", "/").strip("/")
        if norm_remote == "/":
            QMessageBox.warning(
                self,
                "Invalid Remote Folder",
                "Please specify a folder name or subfolder path (e.g. /Backups/Project).",
            )
            return

        remote_id = self._resolved_remote_id
        if not remote_id and self.drive.has_valid_token():
            try:
                remote_id = self.drive.ensure_remote_path(norm_remote)
            except Exception as exc:
                logger.warning("Deferred remote folder creation to sync pass: %s", exc)

        selected_mode = str(self.version_mode_combo.currentData() or "disabled")
        versioning_enabled = selected_mode != "disabled"
        versioning_mode = (
            selected_mode if versioning_enabled else "versions_subdir"
        )

        subpaths = [
            p.strip().strip("/").replace("\\", "/") or "*"
            for p in self.subpaths_edit.text().split(",")
            if p.strip()
        ] or ["*"]

        if self._existing_pair is not None:
            self.result_pair = SyncPair(
                pair_id=self._existing_pair.pair_id,
                local_path=str(local_dir),
                remote_path=norm_remote,
                remote_folder_id=remote_id,
                enabled=self._existing_pair.enabled,
                versioning_enabled=versioning_enabled,
                versioning_mode=versioning_mode,
                versioning_paths=subpaths,
                max_versions_per_file=int(self.max_versions_spin.value()),
            )
        else:
            self.result_pair = SyncPair.create(
                local_path=str(local_dir),
                remote_path=norm_remote,
                remote_folder_id=remote_id,
                enabled=True,
                versioning_enabled=versioning_enabled,
                versioning_mode=versioning_mode,
                versioning_paths=subpaths,
                max_versions_per_file=int(self.max_versions_spin.value()),
            )
        self.accept()
