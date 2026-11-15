"""API mode tab: pick project/site/channels, then build arbitrary pairings.

Unlike the legacy implementation, pairings are NOT restricted to channels with
identical names. Users add pairs manually, or use "Auto-fill Same Name" as a
convenience for channels that only differ by locale suffix (e.g. ``_en`` / ``_de``).

Interactive-state contract: all mutating controls (project/site selectors,
channel lists, pair operations) are gated through ``_refresh_interactive_state``
so a running comparison or in-flight fetch cannot race against UI edits.
"""

import re
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.models import DownloadPair

from .dialogs import show_message
from .layout_utils import scrollable_container
from .pair_status import PairTableStatus
from .workers import FetchChannelsWorker, FetchProjectsWorker, FetchSitesWorker

# Match a trailing locale suffix of 2-3 letters (case-insensitive), optionally
# before an extension. ``{2,3}`` trades a small false-positive risk (e.g.
# ``_new``) for wider coverage; auto-fill is a convenience, users can adjust.
_LOCALE_SUFFIX_RE = re.compile(r"(_[a-zA-Z]{2,3})+(\.[^.]+)?$")

_CHANNEL_LIST_MIN_HEIGHT = 110
_PAIR_TABLE_MIN_HEIGHT = 150
_PAIR_ROW_HEIGHT = 30


def _base_name(channel_name: str) -> str:
    """Strip trailing locale suffixes so feed_A_en.csv and feed_A_de.csv match."""
    stem = Path(channel_name).stem
    ext = Path(channel_name).suffix
    stripped = _LOCALE_SUFFIX_RE.sub("", stem)
    if not stripped:
        stripped = stem
    return stripped + ext


class ApiModeTab(QWidget):
    """UI for project/site/channel selection and pair configuration.

    Emits ``run_requested`` with a dict:
        {'mode': 'api',
         'pairs': List[DownloadPair],
         'site1_label': str,
         'site2_label': str,
         'config': dict}
    """

    log = Signal(str)

    def __init__(
        self,
        base_url: str,
        headers: dict,
        verify_ssl: bool,
        parent=None,
    ):
        super().__init__(parent)
        self.setObjectName("ApiModeTab")
        self._base_url = base_url
        self._headers = headers
        self._verify_ssl = verify_ssl

        self._projects: List[dict] = []
        self._sites1_all: List[dict] = []
        self._sites2_all: List[dict] = []
        self._channels1: List[dict] = []
        self._channels2: List[dict] = []
        self._pairs: List[DownloadPair] = []
        self._pair_seq = 0

        self._is_running = False
        self._pending_channel_fetches = 0

        # Live worker references; QThread objects must outlive their run.
        self._worker_projects: Optional[FetchProjectsWorker] = None
        self._worker_sites1: Optional[FetchSitesWorker] = None
        self._worker_sites2: Optional[FetchSitesWorker] = None
        self._worker_ch1: Optional[FetchChannelsWorker] = None
        self._worker_ch2: Optional[FetchChannelsWorker] = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout, _scroll = scrollable_container(self, spacing=8)

        # Project row
        project_group = QGroupBox("Project")
        project_group.setObjectName("projectGroup")
        project_row = QHBoxLayout(project_group)
        project_row.addWidget(QLabel("Project:"))
        self.project_combo = QComboBox()
        self.project_combo.setObjectName("projectCombo")
        self.project_combo.setMinimumWidth(220)
        project_row.addWidget(self.project_combo, stretch=1)
        self.refresh_projects_btn = QPushButton("Refresh Projects")
        self.refresh_projects_btn.setObjectName("refreshProjectsBtn")
        self.refresh_projects_btn.clicked.connect(
            lambda checked=False: self._on_refresh_projects()
        )
        project_row.addWidget(self.refresh_projects_btn)
        layout.addWidget(project_group)

        # Sites row (side by side)
        sites_row = QHBoxLayout()
        sites_row.setSpacing(8)
        sites_row.addWidget(self._build_site_group(site_num=1), stretch=1)
        sites_row.addWidget(self._build_site_group(site_num=2), stretch=1)
        layout.addLayout(sites_row)

        # Channels + pairing operations
        channel_group = QGroupBox("Channels")
        channel_group.setObjectName("channelGroup")
        channel_layout = QVBoxLayout(channel_group)
        channel_layout.setSpacing(8)

        channel_dual = QHBoxLayout()
        channel_dual.setSpacing(8)

        left_side = QVBoxLayout()
        left_title = QLabel("Site 1 Channels")
        left_title.setObjectName("leftChannelsTitle")
        left_side.addWidget(left_title)
        self.channel_list1 = QListWidget()
        self.channel_list1.setObjectName("channelList1")
        self.channel_list1.setSelectionMode(QAbstractItemView.SingleSelection)
        self.channel_list1.setMinimumHeight(_CHANNEL_LIST_MIN_HEIGHT)
        left_side.addWidget(self.channel_list1, stretch=1)
        channel_dual.addLayout(left_side)

        right_side = QVBoxLayout()
        right_title = QLabel("Site 2 Channels")
        right_title.setObjectName("rightChannelsTitle")
        right_side.addWidget(right_title)
        self.channel_list2 = QListWidget()
        self.channel_list2.setObjectName("channelList2")
        self.channel_list2.setSelectionMode(QAbstractItemView.SingleSelection)
        self.channel_list2.setMinimumHeight(_CHANNEL_LIST_MIN_HEIGHT)
        right_side.addWidget(self.channel_list2, stretch=1)
        channel_dual.addLayout(right_side)

        channel_layout.addLayout(channel_dual)

        # One toolbar for every channel / pairing operation. Two separate
        # button rows cost ~44px of height in a card that already had to
        # scroll at 720p, and they read as one workflow anyway.
        pair_ops = QHBoxLayout()
        pair_ops.setSpacing(8)
        self.fetch_channels_btn = QPushButton("Fetch Channels")
        self.fetch_channels_btn.setObjectName("fetchChannelsBtn")
        self.fetch_channels_btn.clicked.connect(
            lambda checked=False: self._on_fetch_channels()
        )
        pair_ops.addWidget(self.fetch_channels_btn)

        self.auto_fill_btn = QPushButton("Auto-fill Same Name")
        self.auto_fill_btn.setObjectName("autoFillBtn")
        self.auto_fill_btn.clicked.connect(lambda checked=False: self._on_auto_fill())
        pair_ops.addWidget(self.auto_fill_btn)

        self.add_pair_btn = QPushButton("+ Add Selected Pair")
        self.add_pair_btn.setObjectName("addPairBtn")
        self.add_pair_btn.clicked.connect(lambda checked=False: self._on_add_pair())
        pair_ops.addWidget(self.add_pair_btn)

        self.clear_pairs_btn = QPushButton("Clear All")
        self.clear_pairs_btn.setObjectName("clearPairsBtn")
        self.clear_pairs_btn.clicked.connect(
            lambda checked=False: self._on_clear_pairs()
        )
        pair_ops.addWidget(self.clear_pairs_btn)

        pair_ops.addStretch()
        self.pair_count_label = QLabel("0 pairs")
        self.pair_count_label.setObjectName("pairCountLabel")
        pair_ops.addWidget(self.pair_count_label)
        channel_layout.addLayout(pair_ops)

        layout.addWidget(channel_group)

        # Pairing table
        pair_group = QGroupBox("Pairing")
        pair_group.setToolTip("The two sides may use different channel names.")
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

        layout.addStretch()

    def _build_site_group(self, site_num: int) -> QGroupBox:
        title = "Site 1 (Baseline)" if site_num == 1 else "Site 2 (Comparison)"
        group = QGroupBox(title)
        group.setObjectName(f"site{site_num}Group")
        grid = QGridLayout(group)
        grid.setHorizontalSpacing(6)
        grid.setVerticalSpacing(6)

        grid.addWidget(QLabel("Site:"), 0, 0)
        combo = QComboBox()
        combo.setObjectName(f"site{site_num}Combo")
        combo.setMinimumWidth(180)
        grid.addWidget(combo, 0, 1, 1, 2)

        fetch_btn = QPushButton("Fetch")
        fetch_btn.setObjectName(f"fetchSites{site_num}Btn")
        fetch_btn.clicked.connect(
            lambda checked=False, n=site_num: self._on_fetch_sites(n)
        )
        grid.addWidget(fetch_btn, 0, 3)

        search_edit = QLineEdit()
        search_edit.setObjectName(f"site{site_num}Search")
        search_edit.setPlaceholderText("Filter sites...")
        search_edit.textChanged.connect(
            lambda text, n=site_num: self._filter_sites(n, text)
        )
        grid.addWidget(search_edit, 1, 0, 1, 4)

        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 1)

        if site_num == 1:
            self.site1_combo = combo
            self.site1_search = search_edit
            self.fetch_sites1_btn = fetch_btn
        else:
            self.site2_combo = combo
            self.site2_search = search_edit
            self.fetch_sites2_btn = fetch_btn
        return group

    # ------------------------------------------------------------------
    # Centralised interactive-state management
    # ------------------------------------------------------------------

    def _refresh_interactive_state(self) -> None:
        """Enable/disable every mutating control based on current activity.

        Running state or in-flight fetches must not allow the user to edit
        anything that would race against the worker.
        """
        running = self._is_running
        fetching_channels = self._pending_channel_fetches > 0
        fetching_projects = (
            self._worker_projects is not None and self._worker_projects.isRunning()
        )
        fetching_sites1 = (
            self._worker_sites1 is not None and self._worker_sites1.isRunning()
        )
        fetching_sites2 = (
            self._worker_sites2 is not None and self._worker_sites2.isRunning()
        )
        can_edit_pairs = not running and not fetching_channels

        self.refresh_projects_btn.setEnabled(not running and not fetching_projects)
        self.fetch_channels_btn.setEnabled(not running and not fetching_channels)
        self.project_combo.setEnabled(not running)
        self.site1_combo.setEnabled(not running)
        self.site2_combo.setEnabled(not running)
        self.site1_search.setEnabled(not running)
        self.site2_search.setEnabled(not running)
        self.fetch_sites1_btn.setEnabled(not running and not fetching_sites1)
        self.fetch_sites2_btn.setEnabled(not running and not fetching_sites2)
        self.channel_list1.setEnabled(not running)
        self.channel_list2.setEnabled(not running)
        self.add_pair_btn.setEnabled(can_edit_pairs)
        self.auto_fill_btn.setEnabled(can_edit_pairs)
        self.clear_pairs_btn.setEnabled(can_edit_pairs)
        self._render_pairs()

    # ------------------------------------------------------------------
    # Worker: projects
    # ------------------------------------------------------------------

    def _on_refresh_projects(self) -> None:
        if self._is_running:
            return
        self._worker_projects = FetchProjectsWorker(
            self._base_url,
            self._headers,
            self._verify_ssl,
        )
        self._worker_projects.log.connect(self.log.emit)
        self._worker_projects.data_ready.connect(self._on_projects_received)
        self._worker_projects.error.connect(self._on_worker_error)
        self._worker_projects.finished.connect(self._worker_projects.deleteLater)
        self._worker_projects.start()
        self._refresh_interactive_state()

    def _on_projects_received(self, projects: List[dict]) -> None:
        self._projects = projects
        self.project_combo.clear()
        for p in projects:
            self.project_combo.addItem(f"{p['name']} (ID: {p['id']})", p["id"])
        self._refresh_interactive_state()

    def _on_worker_error(self, message: str) -> None:
        self.log.emit(f"Error: {message}")
        self._refresh_interactive_state()

    # ------------------------------------------------------------------
    # Worker: sites
    # ------------------------------------------------------------------

    def _on_fetch_sites(self, site_num: int) -> None:
        if self._is_running:
            return
        project_id = self.project_combo.currentData()
        if not project_id:
            show_message(
                self,
                "No Project",
                "Select a project first.",
                QMessageBox.Warning,
            )
            return
        worker = FetchSitesWorker(
            self._base_url,
            self._headers,
            project_id,
            only_active=False,
            verify_ssl=self._verify_ssl,
        )
        worker.log.connect(self.log.emit)
        worker.data_ready.connect(
            lambda sites, n=site_num: self._on_sites_received(sites, n)
        )
        worker.error.connect(self._on_worker_error)
        worker.finished.connect(worker.deleteLater)
        if site_num == 1:
            self._worker_sites1 = worker
        else:
            self._worker_sites2 = worker
        worker.start()
        self._refresh_interactive_state()

    def _on_sites_received(self, sites: List[dict], site_num: int) -> None:
        if site_num == 1:
            self._sites1_all = sites
            self._populate_site_combo(self.site1_combo, sites, "")
        else:
            self._sites2_all = sites
            self._populate_site_combo(self.site2_combo, sites, "")
        self._refresh_interactive_state()

    def _populate_site_combo(
        self, combo: QComboBox, sites: List[dict], filter_text: str
    ) -> None:
        # Preserve the current selection across a filter keystroke, otherwise
        # typing in the search box silently resets the chosen site.
        previous = combo.currentData()
        combo.blockSignals(True)
        combo.clear()
        needle = filter_text.strip().lower()
        restore_index = -1
        for s in sites:
            name = s["name"]
            sid = str(s["id"])
            if needle and needle not in name.lower() and needle not in sid:
                continue
            combo.addItem(f"{name} (ID: {sid})", s["id"])
            if s["id"] == previous:
                restore_index = combo.count() - 1
        if restore_index >= 0:
            combo.setCurrentIndex(restore_index)
        combo.blockSignals(False)

    def _filter_sites(self, site_num: int, text: str) -> None:
        if site_num == 1:
            self._populate_site_combo(self.site1_combo, self._sites1_all, text)
        else:
            self._populate_site_combo(self.site2_combo, self._sites2_all, text)

    # ------------------------------------------------------------------
    # Worker: channels
    # ------------------------------------------------------------------

    def _on_fetch_channels(self) -> None:
        if self._is_running or self._pending_channel_fetches > 0:
            return
        site1_id = self.site1_combo.currentData()
        site2_id = self.site2_combo.currentData()
        if not site1_id or not site2_id:
            show_message(
                self,
                "No Sites",
                "Select both sites before fetching channels.",
                QMessageBox.Warning,
            )
            return

        self._pending_channel_fetches = 2

        # Clear channels and pairs; stale pairs would reference channels no
        # longer present after the re-fetch. Use the internal variant because
        # the public _on_clear_pairs rejects calls during runs.
        self._channels1 = []
        self._channels2 = []
        self.channel_list1.clear()
        self.channel_list2.clear()
        self._clear_pairs_internal()
        self.log.emit("Fetching channels for both sites...")
        self._refresh_interactive_state()

        w1 = FetchChannelsWorker(
            self._base_url,
            self._headers,
            site1_id,
            verify_ssl=self._verify_ssl,
        )
        w1.log.connect(self.log.emit)
        w1.data_ready.connect(lambda ch: self._on_channels_received(ch, 1))
        w1.error.connect(lambda e: self._on_channel_error(e, 1))
        w1.finished.connect(w1.deleteLater)

        w2 = FetchChannelsWorker(
            self._base_url,
            self._headers,
            site2_id,
            verify_ssl=self._verify_ssl,
        )
        w2.log.connect(self.log.emit)
        w2.data_ready.connect(lambda ch: self._on_channels_received(ch, 2))
        w2.error.connect(lambda e: self._on_channel_error(e, 2))
        w2.finished.connect(w2.deleteLater)

        self._worker_ch1 = w1
        self._worker_ch2 = w2
        w1.start()
        w2.start()

    def _on_channels_received(self, channels: List[dict], which: int) -> None:
        if which == 1:
            self._channels1 = channels
            self._populate_channel_list(self.channel_list1, channels)
        else:
            self._channels2 = channels
            self._populate_channel_list(self.channel_list2, channels)
        self.log.emit(
            f"Loaded {len(self._channels1)} site1 channel(s), "
            f"{len(self._channels2)} site2 channel(s)."
        )
        self._mark_channel_fetch_done()

    def _on_channel_error(self, message: str, which: int) -> None:
        self.log.emit(f"Error fetching site{which} channels: {message}")
        self._mark_channel_fetch_done()

    def _mark_channel_fetch_done(self) -> None:
        self._pending_channel_fetches = max(0, self._pending_channel_fetches - 1)
        self._refresh_interactive_state()

    def _populate_channel_list(
        self, list_widget: QListWidget, channels: List[dict]
    ) -> None:
        list_widget.clear()
        for ch in channels:
            item = QListWidgetItem()
            item.setText(ch["name"])
            item.setData(Qt.UserRole, ch)
            list_widget.addItem(item)

    # ------------------------------------------------------------------
    # Pair table
    # ------------------------------------------------------------------

    def _on_add_pair(self) -> None:
        if self._is_running:
            return
        left_item = self.channel_list1.currentItem()
        right_item = self.channel_list2.currentItem()
        if not left_item or not right_item:
            show_message(
                self,
                "No Selection",
                "Select one channel from each list first.",
                QMessageBox.Warning,
            )
            return
        left_ch = left_item.data(Qt.UserRole)
        right_ch = right_item.data(Qt.UserRole)
        if not left_ch.get("link") or not right_ch.get("link"):
            show_message(
                self,
                "Missing URL",
                "One of the selected channels has no downloadable URL.",
                QMessageBox.Warning,
            )
            return
        self._append_pair(left_ch, right_ch)

    def _append_pair(self, left_ch: dict, right_ch: dict) -> None:
        # A monotonic counter keeps ids unique after removals; deriving the id
        # from len(self._pairs) produced duplicates as soon as a row was
        # deleted from the middle of the list.
        self._pair_seq += 1
        self._pairs.append(
            DownloadPair(
                pair_id=f"pair_{self._pair_seq:04d}",
                left_name=left_ch["name"],
                left_url=left_ch["link"],
                right_name=right_ch["name"],
                right_url=right_ch["link"],
            )
        )
        self._render_pairs()

    def _on_auto_fill(self) -> None:
        if self._is_running:
            return
        if not self._channels1 or not self._channels2:
            show_message(
                self,
                "No Channels",
                "Fetch channels for both sites before auto-filling.",
                QMessageBox.Warning,
            )
            return
        right_by_base: Dict[str, dict] = {}
        for ch in self._channels2:
            key = _base_name(ch["name"])
            if key not in right_by_base:
                right_by_base[key] = ch
        added = 0
        for ch1 in self._channels1:
            key = _base_name(ch1["name"])
            ch2 = right_by_base.get(key)
            if not ch2:
                continue
            if not ch1.get("link") or not ch2.get("link"):
                continue
            self._append_pair(ch1, ch2)
            added += 1
        if added == 0:
            show_message(
                self,
                "No Matches",
                "No channels share the same base name across sites.",
                QMessageBox.Information,
            )
        else:
            self.log.emit(f"Auto-filled {added} pair(s) by base name.")

    def _on_clear_pairs(self) -> None:
        if self._is_running:
            return
        self._clear_pairs_internal()

    def _clear_pairs_internal(self) -> None:
        self._pairs = []
        self._pair_seq = 0
        self._render_pairs()

    def _render_pairs(self) -> None:
        can_edit = not self._is_running and self._pending_channel_fetches == 0
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
                remove_btn.setEnabled(can_edit)
                remove_btn.clicked.connect(
                    lambda checked=False, r=row: self._remove_pair(r)
                )
                self.pair_table.setCellWidget(row, 4, remove_btn)
        self.pair_count_label.setText(
            f"{len(self._pairs)} pair" + ("" if len(self._pairs) == 1 else "s")
        )

    def _remove_pair(self, row: int) -> None:
        if self._is_running or self._pending_channel_fetches > 0:
            return
        if 0 <= row < len(self._pairs):
            del self._pairs[row]
            self._render_pairs()

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def build_run_payload(self, config):
        """Validate the form and return the run request, or None if invalid."""
        if self._is_running or self._pending_channel_fetches > 0:
            return None
        if not self._pairs:
            show_message(
                self,
                "No Pairs",
                "Add at least one pair before starting.",
                QMessageBox.Warning,
            )
            return None
        site1_label = self.site1_combo.currentText().split(" (ID:")[0] or "Site 1"
        site2_label = self.site2_combo.currentText().split(" (ID:")[0] or "Site 2"
        return {
            "mode": "api",
            "pairs": list(self._pairs),
            "site1_label": site1_label,
            "site2_label": site2_label,
            "config": config,
        }

    def set_running(self, running: bool) -> None:
        self._is_running = running
        if running:
            self.pair_status.begin(len(self._pairs))
        else:
            self.pair_status.end()
        self._refresh_interactive_state()

    def update_pair_status(self, status) -> None:
        """Apply a live download status to its pairing-table row."""
        self.pair_status.apply(status)
