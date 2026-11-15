"""Modal dialogs and message helpers.

Visual styles are driven by ``styles/main.qss`` via ``setObjectName`` on each
widget. The few inline styles kept here are for runtime state (e.g. list item
height) that does not fit a static stylesheet.
"""
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from core.logger import DebugLogger
from core.reader import FileReader


class ColumnSelectorDialog(QDialog):
    """Let the user choose which columns participate in comparison.

    Checked-state is kept in ``_checked_state`` rather than read back from the
    list widget so that search filtering does not discard the user's selections.

    Implementation note: items are native checkable ``QListWidgetItem``s rather
    than a ``QCheckBox`` set as a cell widget. A checkbox widget per column
    costs a full QWidget (layout, style, paint) each; for feeds with hundreds
    of columns that made filtering on every keystroke visibly slow.

    Performance note: CSV / Excel only read the header row, so the dialog
    returns in milliseconds even for large files. XML / ZIP require structural
    parsing and may block the UI for several seconds on very large inputs.
    """

    def __init__(self, parent, file_path: str,
                 currently_selected: Optional[List[str]] = None):
        super().__init__(parent)
        self.setObjectName("ColumnSelectorDialog")
        self.setWindowTitle('Select Columns to Compare')
        self.setMinimumSize(500, 550)
        self.selected_columns: List[str] = []
        self.currently_selected = currently_selected or []
        self.all_columns: List[str] = []
        self._checked_state: Dict[str, bool] = {}
        self._populating = False

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_label = QLabel("Select Columns to Compare")
        title_label.setObjectName("titleLabel")
        layout.addWidget(title_label)

        file_label = QLabel(f"Sample file: {Path(file_path).name}")
        file_label.setObjectName("fileLabel")
        file_label.setWordWrap(True)
        layout.addWidget(file_label)

        desc_label = QLabel(
            "Check the columns you want to compare. Unchecked columns will be ignored."
        )
        desc_label.setObjectName("descLabel")
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("searchEdit")
        self.search_edit.setPlaceholderText("Filter columns...")
        self.search_edit.textChanged.connect(self._filter_columns)
        search_layout.addWidget(self.search_edit)
        layout.addLayout(search_layout)

        select_btns = QHBoxLayout()
        select_all_btn = QPushButton("Select All")
        select_all_btn.setObjectName("selectAllBtn")
        select_all_btn.clicked.connect(lambda: self._toggle_all(True))
        select_btns.addWidget(select_all_btn)
        deselect_all_btn = QPushButton("Deselect All")
        deselect_all_btn.setObjectName("deselectAllBtn")
        deselect_all_btn.clicked.connect(lambda: self._toggle_all(False))
        select_btns.addWidget(deselect_all_btn)
        select_btns.addStretch()
        layout.addLayout(select_btns)

        self.column_list = QListWidget()
        self.column_list.setObjectName("columnList")
        self.column_list.setSelectionMode(QAbstractItemView.NoSelection)
        self.column_list.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.column_list, stretch=1)

        self.stats_label = QLabel()
        self.stats_label.setObjectName("statsLabel")
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("cancelBtn")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        ok_btn = QPushButton("Apply Selection")
        ok_btn.setObjectName("okBtn")
        ok_btn.clicked.connect(self._apply_selection)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._load_columns(file_path)

    def _load_columns(self, file_path: str) -> None:
        try:
            reader = FileReader(DebugLogger())
            columns = reader.read_columns(file_path)
            if not columns:
                raise ValueError("The file exposes no columns.")
            self.all_columns = list(columns)
            for col in self.all_columns:
                if self.currently_selected:
                    self._checked_state[col] = col in self.currently_selected
                else:
                    self._checked_state[col] = True
            self._populate_list(self.all_columns)
        except Exception as e:
            QMessageBox.warning(self, "Error", f"Failed to read file:\n{str(e)}")
            self.reject()

    def _populate_list(self, columns: List[str]) -> None:
        self._populating = True
        try:
            self.column_list.clear()
            for col in columns:
                item = QListWidgetItem(col)
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                item.setCheckState(
                    Qt.Checked if self._checked_state.get(col, True) else Qt.Unchecked
                )
                self.column_list.addItem(item)
        finally:
            self._populating = False
        self._update_stats()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._populating:
            return
        self._checked_state[item.text()] = item.checkState() == Qt.Checked
        self._update_stats()

    def _filter_columns(self, text: str) -> None:
        search_text = text.lower().strip()
        if not search_text:
            self._populate_list(self.all_columns)
            return
        filtered = [c for c in self.all_columns if search_text in c.lower()]
        self._populate_list(filtered)

    def _toggle_all(self, checked: bool) -> None:
        """Toggle only the currently visible rows; hidden selections persist."""
        self._populating = True
        try:
            for i in range(self.column_list.count()):
                item = self.column_list.item(i)
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                self._checked_state[item.text()] = checked
        finally:
            self._populating = False
        self._update_stats()

    def _update_stats(self) -> None:
        total = len(self.all_columns)
        checked = sum(1 for c in self.all_columns if self._checked_state.get(c, True))
        self.stats_label.setText(f"Selected: {checked} / {total} columns")

    def _apply_selection(self) -> None:
        self.selected_columns = [
            col for col in self.all_columns
            if self._checked_state.get(col, True)
        ]
        self.accept()

    def get_selected_columns(self) -> List[str]:
        return self.selected_columns


class CompletionDialog(QDialog):
    """Post-validation summary popup.

    ``summary_data`` keys consumed (all optional, default 0):
      - total_pairs        (int)   total pairs attempted
      - files              (int)   pairs successfully compared
      - skipped            (int)   pairs skipped (no SKU column found)
      - validation_failed  (int)   validator-level failures
      - download_failed    (int)   download-level failures
      - match_rate         (float) 0-100 overall average across columns
      - diffs              (int)   total cell-level differences
      - report_path        (str)   auto-exported zip, if any

    The caller (MainWindow) is responsible for assembling this dict from the
    merged worker result; passing ``results['overall']`` directly works as long
    as ``files`` is remapped from ``overall['success']``.
    """

    def __init__(self, parent, title: str, summary_data: dict):
        super().__init__(parent)
        self.setObjectName("CompletionDialog")
        self.setWindowTitle(title)
        self.setMinimumWidth(550)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_label = QLabel("Validation Complete")
        title_label.setObjectName("titleLabel")
        layout.addWidget(title_label)

        total = summary_data.get("total_pairs", 0)
        success = summary_data.get("files", 0)
        skipped = summary_data.get("skipped", 0)
        validation_failed = summary_data.get("validation_failed", 0)
        download_failed = summary_data.get("download_failed", 0)

        info_text = (
            f"<b>Files Processed:</b> {total}<br>"
            f"<b>Successfully compared:</b> {success}<br>"
            f"<b>Skipped (no SKU column):</b> {skipped}<br>"
            f"<b>Download failed:</b> {download_failed}<br>"
            f"<b>Validation failed:</b> {validation_failed}<br>"
            f"<br>"
            f"<b>Overall Match Rate:</b> {summary_data.get('match_rate', 0)}%<br>"
            f"<b>Total Differences:</b> {summary_data.get('diffs', 0):,}<br>"
        )
        report_path = summary_data.get("report_path")
        if report_path:
            info_text += f"<br><b>Report saved to:</b><br>{report_path}<br>"
        info_text += "<br><i>Check the \"Debug Log\" tab for details.</i>"

        info_label = QLabel(info_text)
        info_label.setObjectName("infoLabel")
        info_label.setWordWrap(True)
        info_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(info_label)

        btn_layout = QHBoxLayout()
        view_btn = QPushButton("View Results")
        view_btn.setObjectName("okBtn")
        view_btn.clicked.connect(self.accept)
        btn_layout.addWidget(view_btn)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("cancelBtn")
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)


def show_message(parent, title: str, message: str,
                 icon: QMessageBox.Icon = QMessageBox.Information) -> int:
    """Thin wrapper around QMessageBox; styling comes from main.qss."""
    msg_box = QMessageBox(parent)
    msg_box.setObjectName("AppMessageBox")
    msg_box.setWindowTitle(title)
    msg_box.setText(message)
    msg_box.setIcon(icon)
    return msg_box.exec()


def show_question(parent, title: str, message: str,
                  default_no: bool = True) -> bool:
    """Yes/No confirmation. Returns True only on an explicit Yes.

    Defaulting to No keeps a destructive action (replacing a hand-built
    pairing) from being confirmed by a stray Enter key.
    """
    msg_box = QMessageBox(parent)
    msg_box.setObjectName("AppMessageBox")
    msg_box.setWindowTitle(title)
    msg_box.setText(message)
    msg_box.setIcon(QMessageBox.Question)
    msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
    msg_box.setDefaultButton(QMessageBox.No if default_no else QMessageBox.Yes)
    return msg_box.exec() == QMessageBox.Yes
