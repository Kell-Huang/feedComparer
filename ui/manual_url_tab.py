"""Manual URL tab: paste two lists of URLs, then pair them by order or by hand.

Pairing model
-------------
``_left_nodes`` / ``_right_nodes`` hold the deduplicated URL lists and
``_links`` holds ``(left_index, right_index)`` tuples. ``_pairs`` is derived
from those three, and both views render that same list — there is no second
copy of the pairing state to drift out of sync.

Generating is only the starting point. The map view lets the user drag a port
from one file to another to re-pair (or to disconnect), while the table view
keeps the compact list and the live per-pair status column during a run.

Deduplication matters here: the same URL pasted twice becomes one node, and
one node may hold several links. That is how 1-to-many pairings are expressed,
and the downloader already fetches each distinct URL once.
"""

from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlparse

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import DownloadPair
from core.progress import (
    PAIR_CANCELLED,
    PAIR_DONE,
    PAIR_DOWNLOADING,
    PAIR_FAILED,
    PAIR_PENDING,
)

from .config_widget import ComparisonConfigWidget
from .dialogs import show_message, show_question
from .layout_utils import scrollable_container
from .pair_status import PairTableStatus
from .pairing_canvas import PairingCanvas

_URL_EDIT_MIN_HEIGHT = 110
_PAIR_TABLE_MIN_HEIGHT = 150
_PAIR_ROW_HEIGHT = 30

# Canvas state -> the palette keys used by the drawing code.
_CANVAS_STATES = {
    PAIR_PENDING: "queued",
    PAIR_DOWNLOADING: "downloading",
    PAIR_DONE: "done",
    PAIR_FAILED: "failed",
    PAIR_CANCELLED: "failed",
}


def _infer_name_from_url(url: str, fallback_index: int) -> str:
    """Infer a display name from a URL. Falls back to ``url_N`` if empty."""
    try:
        parsed = urlparse(url)
        name = Path(parsed.path.rstrip("/")).name
        if name:
            return name
    except Exception:
        pass
    return f"url_{fallback_index}"


def _dedupe(values: List[str]) -> List[str]:
    """Drop repeats while keeping the order the user pasted them in."""
    seen = set()
    unique = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


class ManualUrlTab(QWidget):
    """UI for pasting two URL lists and pairing them.

    ``build_run_payload()`` returns::

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
        self._left_nodes: List[str] = []
        self._right_nodes: List[str] = []
        self._links: List[Tuple[int, int]] = []
        self._is_running = False
        self._build_ui(exclude_keywords)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self, exclude_keywords) -> None:
        layout, _scroll = scrollable_container(self)

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
            "https://example.com/feed_a.csv\n" "https://example.com/feed_b.csv"
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
            "https://example.com/feed_a.csv\n" "https://example.com/feed_b.csv"
        )
        self.url_right_edit.textChanged.connect(self._update_counts)
        right_side.addWidget(self.url_right_edit, stretch=1)
        dual.addLayout(right_side)

        url_layout.addLayout(dual)

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

        pair_group = QGroupBox("Pairing")
        pair_group.setToolTip(
            "Drag a port to the other side to connect two files; drag it onto "
            "the same file again to disconnect."
        )
        pair_group.setObjectName("pairGroup")
        pair_layout = QVBoxLayout(pair_group)
        pair_layout.setSpacing(8)

        view_row = QHBoxLayout()
        view_row.setSpacing(6)
        self.view_map_btn = QPushButton("Map")
        self.view_table_btn = QPushButton("Table")
        self._view_group = QButtonGroup(self)
        self._view_group.setExclusive(True)
        for button, checked in (
            (self.view_map_btn, True),
            (self.view_table_btn, False),
        ):
            button.setObjectName("pairViewBtn")
            button.setCheckable(True)
            button.setChecked(checked)
            button.clicked.connect(lambda _checked=False: self._apply_view())
            self._view_group.addButton(button)
            view_row.addWidget(button)
        view_row.addStretch()

        # Shown when the URL text no longer matches the map that was built from
        # it. The pairing is kept: silently discarding hand-made links because
        # the user edited a line would be worse than a stale map.
        self.stale_label = QLabel(
            "URL list changed — press Generate to rebuild the map."
        )
        self.stale_label.setObjectName("staleLabel")
        self.stale_label.setVisible(False)
        view_row.addWidget(self.stale_label)
        pair_layout.addLayout(view_row)

        self.canvas = PairingCanvas()
        self.canvas.links_changed.connect(self._on_links_changed)
        pair_layout.addWidget(self.canvas)

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

        self.config_widget = ComparisonConfigWidget(exclude_keywords=exclude_keywords)
        self.config_widget.show_file_filter(False)
        layout.addWidget(self.config_widget)

        layout.addStretch()

        # Render the empty state so the card starts at the right height and the
        # view switch stays hidden until there is something to switch between.
        self._rebuild_pairs()
        self._apply_view()

    def _apply_view(self) -> None:
        """Show the map or the table; both read the same pair list."""
        show_map = self.view_map_btn.isChecked()
        self.canvas.setVisible(show_map)
        self.pair_table.setVisible(not show_map)

    # ------------------------------------------------------------------
    # URL parsing & pairing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_urls(text: str) -> List[str]:
        return [line.strip() for line in text.split("\n") if line.strip()]

    def _update_counts(self) -> None:
        left_urls = self._parse_urls(self.url_left_edit.toPlainText())
        right_urls = self._parse_urls(self.url_right_edit.toPlainText())
        self.left_count_label.setText(self._count_text("Site 1 URLs", left_urls))
        self.right_count_label.setText(self._count_text("Site 2 URLs", right_urls))
        self._check_stale()

    @staticmethod
    def _count_text(prefix: str, urls: List[str]) -> str:
        total = len(urls)
        unique = len(_dedupe(urls))
        text = f"{prefix} — {total} line" + ("" if total == 1 else "s")
        if unique != total:
            text += f" · {unique} unique"
        return text

    def _on_generate_pairs(self) -> None:
        if self._is_running:
            return
        left_urls = self._parse_urls(self.url_left_edit.toPlainText())
        right_urls = self._parse_urls(self.url_right_edit.toPlainText())
        if not left_urls or not right_urls:
            show_message(
                self,
                "Empty Side",
                "Paste URLs on both sides before generating pairs.",
                QMessageBox.Warning,
            )
            return

        if self._links and not show_question(
            self,
            "Replace Pairing",
            "Generating again replaces the current pairing, including any links "
            "you made by hand.\n\nContinue?",
        ):
            return

        left_nodes = _dedupe(left_urls)
        right_nodes = _dedupe(right_urls)
        count = min(len(left_nodes), len(right_nodes))

        self._left_nodes = left_nodes
        self._right_nodes = right_nodes
        self._links = [(index, index) for index in range(count)]
        self.stale_label.setVisible(False)
        self._rebuild_pairs()

        merged_left = len(left_urls) - len(left_nodes)
        merged_right = len(right_urls) - len(right_nodes)
        if (len(left_urls) != len(right_urls)) or merged_left or merged_right:
            show_message(
                self,
                "Count Mismatch",
                self._mismatch_text(
                    left_urls, right_urls, merged_left, merged_right, count
                ),
                QMessageBox.Information,
            )

    @staticmethod
    def _mismatch_text(left_urls, right_urls, merged_left, merged_right, count) -> str:
        parts = [f"Site 1 has {len(left_urls)} URL(s), Site 2 has {len(right_urls)}."]
        merged = []
        if merged_left:
            merged.append(f"{merged_left} duplicate(s) on Site 1")
        if merged_right:
            merged.append(f"{merged_right} duplicate(s) on Site 2")
        if merged:
            parts.append("Merged " + " and ".join(merged) + " into single nodes.")
        parts.append(f"Generated {count} pair(s) by position.")
        parts.append(
            "Drag a port in the map to pair the leftovers, or repeat a URL to "
            "build a one-to-many pairing."
        )
        return "\n\n".join(parts)

    def _on_clear_pairs(self) -> None:
        if self._is_running:
            return
        self._left_nodes = []
        self._right_nodes = []
        self._links = []
        self.stale_label.setVisible(False)
        self._rebuild_pairs()

    def _on_links_changed(self, links) -> None:
        """The canvas edited the pairing; adopt it and refresh both views."""
        if self._is_running:
            return
        self._links = [tuple(link) for link in links]
        self._rebuild_pairs()

    def _rebuild_pairs(self) -> None:
        """Regenerate ``_pairs`` from nodes + links, then refresh both views."""
        self._pairs = []
        for position, (left_index, right_index) in enumerate(self._links, start=1):
            left_url = self._left_nodes[left_index]
            right_url = self._right_nodes[right_index]
            self._pairs.append(
                DownloadPair(
                    pair_id=f"pair_{position:04d}",
                    left_name=_infer_name_from_url(left_url, left_index + 1),
                    left_url=left_url,
                    right_name=_infer_name_from_url(right_url, right_index + 1),
                    right_url=right_url,
                )
            )

        self.canvas.set_model(
            [
                _infer_name_from_url(url, index + 1)
                for index, url in enumerate(self._left_nodes)
            ],
            [
                _infer_name_from_url(url, index + 1)
                for index, url in enumerate(self._right_nodes)
            ],
            self._links,
        )
        # With nothing to show, the view switch would be a dead control.
        has_nodes = bool(self._left_nodes or self._right_nodes)
        self.view_map_btn.setVisible(has_nodes)
        self.view_table_btn.setVisible(has_nodes)

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
        if 0 <= row < len(self._links):
            del self._links[row]
            self._rebuild_pairs()

    def _check_stale(self) -> None:
        """Warn when the URL text no longer matches the map built from it."""
        if not self._links:
            self.stale_label.setVisible(False)
            return
        left = _dedupe(self._parse_urls(self.url_left_edit.toPlainText()))
        right = _dedupe(self._parse_urls(self.url_right_edit.toPlainText()))
        self.stale_label.setVisible(
            left != self._left_nodes or right != self._right_nodes
        )

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def build_run_payload(self):
        """Validate the form and return the run request, or None if invalid."""
        if self._is_running:
            return None
        if not self._pairs:
            show_message(
                self,
                "No Pairs",
                "Generate at least one pair before starting.",
                QMessageBox.Warning,
            )
            return None
        return {
            "mode": "manual",
            "pairs": list(self._pairs),
            "site1_label": "Manual Site 1",
            "site2_label": "Manual Site 2",
            "config": self.config_widget.get_config(),
        }

    def set_running(self, running: bool) -> None:
        """Disable URL editing and pair mutation while a run is in progress."""
        self._is_running = running
        self.url_left_edit.setEnabled(not running)
        self.url_right_edit.setEnabled(not running)
        self.generate_btn.setEnabled(not running)
        self.clear_pairs_btn.setEnabled(not running)
        self.config_widget.set_running(running)
        self.canvas.set_locked(running)
        if running:
            self.canvas.clear_pair_status()
            self.pair_status.begin(len(self._pairs))
        else:
            self.pair_status.end()
        self._render_pairs()

    def update_pair_status(self, status) -> None:
        """Apply a live download status to both views."""
        self.pair_status.apply(status)
        self.canvas.set_pair_status(
            status.index,
            _CANVAS_STATES.get(status.state, ""),
            status.fraction,
        )
