"""Shared comparison configuration widget used by all three mode tabs.

Holds SKU column, priority list, compare columns, excluded columns and the
common flags. The "Select from File" button opens a file picker and reuses
ColumnSelectorDialog, so any mode can populate compare_columns without knowing
the mode's data source.

Layout note: the "Workers" spin box used to sit directly in the grid, so it
absorbed the whole stretch of its column and rendered as a very wide box. The
bottom row is now an HBox with an explicit stretch, which keeps every control
at its natural width regardless of window size.
"""
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from .dialogs import ColumnSelectorDialog


FILE_FILTER_OPTIONS = ['All supported', '*.csv', '*.xlsx', '*.xml', '*.zip']
FILE_DIALOG_FILTER = (
    'All Supported (*.csv *.xlsx *.xls *.xml *.zip);;'
    'CSV (*.csv);;Excel (*.xlsx *.xls);;XML (*.xml);;ZIP (*.zip)'
)


class ComparisonConfigWidget(QWidget):
    """Reusable configuration block. Every mode owns one instance."""

    def __init__(self, exclude_keywords: Optional[List[str]] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("ComparisonConfigWidget")
        self._default_exclude = list(exclude_keywords or [])
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        group = QGroupBox("Column Configuration")
        group.setObjectName("configGroup")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        # Row 0: SKU column + auto-detect
        grid.addWidget(QLabel("SKU Column:"), 0, 0)
        self.sku_edit = QLineEdit()
        self.sku_edit.setObjectName("skuEdit")
        self.sku_edit.setPlaceholderText("Leave empty for auto-detect")
        grid.addWidget(self.sku_edit, 0, 1, 1, 2)
        self.auto_detect_check = QCheckBox("Auto-detect SKU column")
        self.auto_detect_check.setObjectName("autoDetectCheck")
        self.auto_detect_check.setChecked(True)
        grid.addWidget(self.auto_detect_check, 0, 3)

        # Row 1: priority list
        grid.addWidget(QLabel("Priority List:"), 1, 0, Qt.AlignTop)
        self.priority_edit = QPlainTextEdit()
        self.priority_edit.setObjectName("priorityEdit")
        self.priority_edit.setFixedHeight(64)
        self.priority_edit.setPlaceholderText(
            "One column per line, highest priority first\n"
            "e.g.  ean / at_sku / sku / id"
        )
        grid.addWidget(self.priority_edit, 1, 1, 1, 3)

        # Row 2: compare columns + select from file
        grid.addWidget(QLabel("Compare Columns:"), 2, 0)
        self.cols_edit = QLineEdit()
        self.cols_edit.setObjectName("colsEdit")
        self.cols_edit.setPlaceholderText(
            "e.g. price, stock (empty = all common columns except excluded)"
        )
        grid.addWidget(self.cols_edit, 2, 1, 1, 2)
        self.select_cols_btn = QPushButton("Select from File")
        self.select_cols_btn.setObjectName("selectColsBtn")
        self.select_cols_btn.clicked.connect(self._on_select_from_file)
        grid.addWidget(self.select_cols_btn, 2, 3)

        # Row 3: excluded columns
        grid.addWidget(QLabel("Exclude Columns:"), 3, 0)
        self.exclude_edit = QLineEdit()
        self.exclude_edit.setObjectName("excludeEdit")
        self.exclude_edit.setPlaceholderText(
            "Comma separated keywords; any column whose name contains one is skipped"
        )
        self.exclude_edit.setText(', '.join(self._default_exclude))
        self.exclude_edit.setToolTip(
            "Matching is case-insensitive and substring based.\n"
            "Default excludes stock/price columns; clear the field to compare everything."
        )
        grid.addWidget(self.exclude_edit, 3, 1, 1, 3)

        # Row 4: flags
        flags_row = QWidget()
        flags_layout = QHBoxLayout(flags_row)
        flags_layout.setContentsMargins(0, 0, 0, 0)
        flags_layout.setSpacing(20)
        self.normalize_check = QCheckBox("Normalize SKUs")
        self.normalize_check.setObjectName("normalizeCheck")
        self.normalize_check.setChecked(True)
        flags_layout.addWidget(self.normalize_check)
        self.case_check = QCheckBox("Case Sensitive")
        self.case_check.setObjectName("caseCheck")
        self.case_check.setChecked(True)
        flags_layout.addWidget(self.case_check)
        self.debug_check = QCheckBox("Debug Logging")
        self.debug_check.setObjectName("debugCheck")
        self.debug_check.setChecked(True)
        flags_layout.addWidget(self.debug_check)
        # The per-cell diff file can reach millions of rows on a feed where
        # most cells differ; not every run needs it.
        self.export_diff_check = QCheckBox("Export Differences")
        self.export_diff_check.setObjectName("exportDiffCheck")
        self.export_diff_check.setChecked(True)
        self.export_diff_check.setToolTip(
            "Write the per-cell difference file. Turn it off when you only "
            "need the match rates and the report."
        )
        flags_layout.addWidget(self.export_diff_check)
        flags_layout.addStretch()
        grid.addWidget(flags_row, 4, 0, 1, 4)

        # Row 5: workers + file filter (natural widths, stretch at the end)
        bottom_row = QWidget()
        bottom_layout = QHBoxLayout(bottom_row)
        bottom_layout.setContentsMargins(0, 0, 0, 0)
        bottom_layout.setSpacing(8)
        bottom_layout.addWidget(QLabel("Workers:"))
        self.workers_spin = QSpinBox()
        self.workers_spin.setObjectName("workersSpin")
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(4)
        self.workers_spin.setFixedWidth(70)
        bottom_layout.addWidget(self.workers_spin)
        bottom_layout.addSpacing(20)
        self.filter_label = QLabel("File Filter:")
        bottom_layout.addWidget(self.filter_label)
        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("filterCombo")
        self.filter_combo.addItems(FILE_FILTER_OPTIONS)
        self.filter_combo.setMinimumWidth(150)
        bottom_layout.addWidget(self.filter_combo)
        bottom_layout.addStretch()
        grid.addWidget(bottom_row, 5, 0, 1, 4)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)
        grid.setColumnStretch(3, 0)
        outer.addWidget(group)

    def show_file_filter(self, visible: bool) -> None:
        """The file filter only means something in File mode."""
        self.filter_label.setVisible(visible)
        self.filter_combo.setVisible(visible)

    def _on_select_from_file(self) -> None:
        current = self.cols_edit.text().strip()
        currently_selected = [c.strip() for c in current.split(',') if c.strip()] if current else []
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select a sample file", "", FILE_DIALOG_FILTER,
        )
        if not file_path:
            return
        dialog = ColumnSelectorDialog(self, file_path, currently_selected)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            selected = dialog.get_selected_columns()
            self.cols_edit.setText(', '.join(selected))

    def _current_file_filter(self) -> Optional[str]:
        text = self.filter_combo.currentText()
        if text == 'All supported':
            return None
        return text

    def _priority_list(self) -> Optional[List[str]]:
        raw = self.priority_edit.toPlainText().strip()
        if not raw:
            return None
        items = [line.strip() for line in raw.split('\n') if line.strip()]
        return items or None

    @staticmethod
    def _split_csv_field(raw: str) -> List[str]:
        return [c.strip() for c in raw.split(',') if c.strip()]

    def _compare_columns(self) -> List[str]:
        return self._split_csv_field(self.cols_edit.text().strip())

    def _exclude_keywords(self) -> List[str]:
        return self._split_csv_field(self.exclude_edit.text().strip())

    def get_config(self) -> dict:
        """Return a fresh config dict. Values are snapshotted at call time."""
        return {
            'sku_column': self.sku_edit.text().strip() or None,
            'compare_columns': self._compare_columns(),
            'normalize_sku': self.normalize_check.isChecked(),
            'case_sensitive': self.case_check.isChecked(),
            'debug': self.debug_check.isChecked(),
            'export_diff': self.export_diff_check.isChecked(),
            'auto_detect': self.auto_detect_check.isChecked(),
            'priority_list': self._priority_list(),
            'exclude_keywords': self._exclude_keywords(),
            'workers': self.workers_spin.value(),
            'file_filter': self._current_file_filter(),
        }

    def set_running(self, running: bool) -> None:
        """Disable every input while a run is in flight."""
        for widget in (
            self.sku_edit, self.priority_edit, self.cols_edit, self.exclude_edit,
            self.auto_detect_check, self.normalize_check, self.case_check,
            self.debug_check, self.export_diff_check, self.workers_spin,
            self.filter_combo,
            self.select_cols_btn,
        ):
            widget.setEnabled(not running)
