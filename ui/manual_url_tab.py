"""Manual URL tab: paste two lists of URLs and pair them by order.

This mode never contacts the platform API; it only needs the URLs.
Users can adjust pairings by regenerating with a corrected URL list, or by
deleting rows. 1-to-many is supported by repeating the same URL on one side
before regenerating.
"""
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QGroupBox, QHeaderView, QHBoxLayout, QLabel,
    QMessageBox, QPlainTextEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from core.models import DownloadPair

from .config_widget import ComparisonConfigWidget
from .dialogs import show_message
from .layout_utils import scrollable_container
from .pair_status import PairTableStatus


_URL_EDIT_MIN_HEIGHT = 110
_PAIR_TABLE_MIN_HEIGHT = 150
_PAIR_ROW_HEIGHT = 30


def _infer_name_from_url(url: str, fallback_index: int) -> str:
    """Infer a display name from a URL. Falls back to ``url_N`` if empty."""
    try:
        parsed = urlparse(url)
        name = Path(parsed.path.rstrip('/')).name
        if name:
            return name
    except Exception:
        pass
    return f"url_{fallback_index}"


class ManualUrlTab(QWidget):
    """UI for pasting two URL lists and pairing them in order.

    Emits ``run_requested`` with a dict:
        {'mode': 'manual',
         'pairs': List[DownloadPair],
         'site1_label': 'Manual Site 1',
         'site2_label': 'Manual Site 2',
         'config': dict}
    """
    
    def __init__(self, exclude_keywords=None, parent=None):
        super().__init__(parent)
        self.setObjectName("ManualUrlTab")
        self._pairs: List[DownloadPair] = []
        self._is_running = False
        self._build_ui(exclude_keywords)

    def _build_ui(self, exclude_keywords) -> None:
        layout, _scroll = scrollable_container(self)

        # URL paste group
        url_group = QGroupBox("Paste URLs (one per line)")
        url_group.setObjectName("urlGroup")
        url_layout = QVBoxLayout(url_group)
        url_layout.setSpacing(8)

        dual = QHBoxLayout()
        dual.setSpacing(8)

        left_side = QVBoxLayout()
        self.left_count_label = QLabel("Site 1 URLs — 0 lines")
        self.left_count_label.setObjectName("leftCountLabel")
        left_side.addWidget(self.left_count_label)
        self.url_left_edit = QPlainTextEdit()
        self.url_left_edit.setObjectName("urlLeftEdit")
        self.url_left_edit.setMinimumHeight(_URL_EDIT_MIN_HEIGHT)
        self.url_left_edit.setPlaceholderText(
            "https://example.com/feed_a.csv\n"
            "https://example.com/feed_b.csv"
        )
        self.url_left_edit.textChanged.connect(self._update_counts)
        left_side.addWidget(self.url_left_edit, stretch=1)
        dual.addLayout(left_side)

        right_side = QVBoxLayout()
        self.right_count_label = QLabel("Site 2 URLs — 0 lines")
        self.right_count_label.setObjectName("rightCountLabel")
        right_side.addWidget(self.right_count_label)
        self.url_right_edit = QPlainTextEdit()
        self.url_right_edit.setObjectName("urlRightEdit")
        self.url_right_edit.setMinimumHeight(_URL_EDIT_MIN_HEIGHT)
        self.url_right_edit.setPlaceholderText(
            "https://example.com/feed_a.csv\n"
            "https://example.com/feed_b.csv"
        )
        self.url_right_edit.textChanged.connect(self._update_counts)
        right_side.addWidget(self.url_right_edit, stretch=1)
        dual.addLayout(right_side)

        url_layout.addLayout(dual)

        # Operation buttons
        ops = QHBoxLayout()
        self.generate_btn = QPushButton("Generate Sequential Pairs")
        self.generate_btn.setObjectName("generatePairsBtn")
        self.generate_btn.clicked.connect(self._on_generate_pairs)
        ops.addWidget(self.generate_btn)
        self.clear_pairs_btn = QPushButton("Clear All")
        self.clear_pairs_btn.setObjectName("clearPairsBtn")
        self.clear_pairs_btn.clicked.connect(self._on_clear_pairs)
        ops.addWidget(self.clear_pairs_btn)
        ops.addStretch()
        self.pair_count_label = QLabel("0 pairs")
        self.pair_count_label.setObjectName("pairCountLabel")
        ops.addWidget(self.pair_count_label)
        url_layout.addLayout(ops)

        layout.addWidget(url_group)

        # Pair table
        pair_group = QGroupBox("Pairing")
        pair_group.setToolTip("Adjustable; one-to-many pairings are supported.")
        pair_group.setObjectName("pairGroup")
        pair_layout = QVBoxLayout(pair_group)
        self.pair_table = QTableWidget(0, 5)
        self.pair_table.setObjectName("pairTable")
        self.pair_table.setHorizontalHeaderLabels(
            ["Pair ID", "Left (Baseline)", "↔", "Right (Comparison)", ""]
        )
        self.pair_table.verticalHeader().setVisible(False)
        self.pair_table.verticalHeader().setDefaultSectionSize(_PAIR_ROW_HEIGHT)
        self.pair_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.pair_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.pair_table.setMinimumHeight(_PAIR_TABLE_MIN_HEIGHT)
        header = self.pair_table.horizontalHeader()
        header.setStretchLastSection(False)
        self.pair_table.setColumnWidth(0, 90)
        self.pair_table.setColumnWidth(2, 36)
        self.pair_table.setColumnWidth(4, 110)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        pair_layout.addWidget(self.pair_table)
        self.pair_status = PairTableStatus(self.pair_table, 4)
        layout.addWidget(pair_group)

        # Shared config
        self.config_widget = ComparisonConfigWidget(exclude_keywords=exclude_keywords)
        self.config_widget.show_file_filter(False)
        layout.addWidget(self.config_widget)

        layout.addStretch()

    # ------------------------------------------------------------------
    # URL parsing & pairing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_urls(text: str) -> List[str]:
        return [line.strip() for line in text.split('\n') if line.strip()]

    def _update_counts(self) -> None:
        left_n = len(self._parse_urls(self.url_left_edit.toPlainText()))
        right_n = len(self._parse_urls(self.url_right_edit.toPlainText()))
        self.left_count_label.setText(
            f"Site 1 URLs — {left_n} line" + ("" if left_n == 1 else "s")
        )
        self.right_count_label.setText(
            f"Site 2 URLs — {right_n} line" + ("" if right_n == 1 else "s")
        )

    def _on_generate_pairs(self) -> None:
        if self._is_running:
            return
        left_urls = self._parse_urls(self.url_left_edit.toPlainText())
        right_urls = self._parse_urls(self.url_right_edit.toPlainText())
        if not left_urls or not right_urls:
            show_message(
                self, "Empty Side",
                "Paste URLs on both sides before generating pairs.",
                QMessageBox.Warning,
            )
            return

        n = min(len(left_urls), len(right_urls))
        self._pairs = []
        for i in range(n):
            self._pairs.append(DownloadPair(
                pair_id=f"pair_{i + 1:04d}",
                left_name=_infer_name_from_url(left_urls[i], i + 1),
                left_url=left_urls[i],
                right_name=_infer_name_from_url(right_urls[i], i + 1),
                right_url=right_urls[i],
            ))
        self._render_pairs()

        if len(left_urls) != len(right_urls):
            show_message(
                self, "Count Mismatch",
                (
                    f"Site 1 has {len(left_urls)} URL(s), Site 2 has {len(right_urls)}. "
                    f"Generated {n} pair(s) by position.\n\n"
                    f"To build 1-to-many pairings, repeat the same URL on one side "
                    f"before regenerating."
                ),
                QMessageBox.Information,
            )

    def _on_clear_pairs(self) -> None:
        if self._is_running:
            return
        self._pairs = []
        self._render_pairs()

    def _render_pairs(self) -> None:
        self.pair_table.setRowCount(len(self._pairs))
        for row, pair in enumerate(self._pairs):
            self.pair_table.setItem(row, 0, QTableWidgetItem(pair.pair_id))
            self.pair_table.setItem(row, 1, QTableWidgetItem(pair.left_name))
            arrow = QTableWidgetItem("↔")
            arrow.setTextAlignment(Qt.AlignCenter)
            self.pair_table.setItem(row, 2, arrow)
            self.pair_table.setItem(row, 3, QTableWidgetItem(pair.right_name))
            if self.pair_status.active:
                # The last column doubles as a live status column while a run
                # is in progress; the Remove buttons come back afterwards.
                self.pair_status.render_row(row)
            else:
                remove_btn = QPushButton("Remove")
                remove_btn.setObjectName("removePairBtn")
                remove_btn.setEnabled(not self._is_running)
                remove_btn.clicked.connect(
                    lambda checked=False, r=row: self._remove_pair(r)
                )
                self.pair_table.setCellWidget(row, 4, remove_btn)
        self.pair_count_label.setText(
            f"{len(self._pairs)} pair" + ("" if len(self._pairs) == 1 else "s")
        )

    def _remove_pair(self, row: int) -> None:
        if self._is_running:
            return
        if 0 <= row < len(self._pairs):
            del self._pairs[row]
            self._render_pairs()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def build_run_payload(self):
        """Validate the form and return the run request, or None if invalid."""
        if self._is_running:
            return None
        if not self._pairs:
            show_message(
                self, "No Pairs",
                "Generate at least one pair before starting.",
                QMessageBox.Warning,
            )
            return None
        return {
            'mode': 'manual',
            'pairs': list(self._pairs),
            'site1_label': 'Manual Site 1',
            'site2_label': 'Manual Site 2',
            'config': self.config_widget.get_config(),
        }

    def set_running(self, running: bool) -> None:
        """Disable URL editing and pair mutation while a run is in progress."""
        self._is_running = running
        self.url_left_edit.setEnabled(not running)
        self.url_right_edit.setEnabled(not running)
        self.generate_btn.setEnabled(not running)
        self.clear_pairs_btn.setEnabled(not running)
        self.config_widget.set_running(running)
        if running:
            self.pair_status.begin(len(self._pairs))
        else:
            self.pair_status.end()
        self._render_pairs()

    def update_pair_status(self, status) -> None:
        """Apply a live download status to its pairing-table row."""
        self.pair_status.apply(status)
