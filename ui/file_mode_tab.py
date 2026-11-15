"""File mode tab: pick two folders and run validation directly."""

from PySide6.QtWidgets import (
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QWidget,
)

from .config_widget import ComparisonConfigWidget
from .dialogs import show_message
from .layout_utils import scrollable_container


class FileModeTab(QWidget):
    """UI for baseline vs comparison folder comparison.

    ``build_run_payload()`` returns the dict MainWindow forwards to
    ``_on_run_requested``::

        {'mode': 'file', 'folder1': str, 'folder2': str, 'config': dict}

    The Run / Export / Clear buttons live in MainWindow's persistent action
    bar, not here, so they stay visible while this form scrolls.
    """

    def __init__(self, exclude_keywords=None, parent=None):
        super().__init__(parent)
        self.setObjectName("FileModeTab")
        self._build_ui(exclude_keywords)

    def _build_ui(self, exclude_keywords) -> None:
        layout, _scroll = scrollable_container(self)

        # Folder selection
        folder_group = QGroupBox("Folder Selection")
        folder_group.setObjectName("folderGroup")
        folder_grid = QGridLayout(folder_group)
        folder_grid.setHorizontalSpacing(8)
        folder_grid.setVerticalSpacing(8)

        folder_grid.addWidget(QLabel("Baseline Folder:"), 0, 0)
        self.f1_edit = QLineEdit()
        self.f1_edit.setObjectName("f1Edit")
        self.f1_edit.setPlaceholderText("Folder to use as baseline")
        folder_grid.addWidget(self.f1_edit, 0, 1)
        self.f1_browse = QPushButton("Browse")
        self.f1_browse.setObjectName("browseBtn1")
        self.f1_browse.clicked.connect(lambda checked=False: self._browse(self.f1_edit))
        folder_grid.addWidget(self.f1_browse, 0, 2)

        folder_grid.addWidget(QLabel("Comparison Folder:"), 1, 0)
        self.f2_edit = QLineEdit()
        self.f2_edit.setObjectName("f2Edit")
        self.f2_edit.setPlaceholderText("Folder to compare against baseline")
        folder_grid.addWidget(self.f2_edit, 1, 1)
        self.f2_browse = QPushButton("Browse")
        self.f2_browse.setObjectName("browseBtn2")
        self.f2_browse.clicked.connect(lambda checked=False: self._browse(self.f2_edit))
        folder_grid.addWidget(self.f2_browse, 1, 2)

        folder_grid.setColumnStretch(1, 1)
        layout.addWidget(folder_group)

        # Shared config
        self.config_widget = ComparisonConfigWidget(exclude_keywords=exclude_keywords)
        self.config_widget.show_file_filter(True)
        layout.addWidget(self.config_widget)

        layout.addStretch()

    def _browse(self, edit: QLineEdit) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if folder:
            edit.setText(folder)

    def build_run_payload(self):
        """Validate the form and return the run request, or None if invalid."""
        f1 = self.f1_edit.text().strip()
        f2 = self.f2_edit.text().strip()
        if not f1 or not f2:
            show_message(
                self,
                "Input Error",
                "Please select both folders before starting.",
                QMessageBox.Warning,
            )
            return None
        return {
            "mode": "file",
            "folder1": f1,
            "folder2": f2,
            "config": self.config_widget.get_config(),
        }

    def set_running(self, running: bool) -> None:
        self.f1_edit.setEnabled(not running)
        self.f2_edit.setEnabled(not running)
        self.f1_browse.setEnabled(not running)
        self.f2_browse.setEnabled(not running)
        self.config_widget.set_running(running)
