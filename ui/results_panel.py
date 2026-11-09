"""Results panel: six sub-tabs that render the merged validator output.

Data contract (``render(results)``):

    results['overall']              dict    counters and rates
    results['files']                dict    {display_name: result_dict}
    results['overall_column_stats'] dict
    results['debug_logs']           list    [{time, category, file, message}]
    results['sku_detection_log']    list    [str]
    results['api_log_lines']        list    [str]   (optional; API log)
    results['site1_label']          str     optional
    results['site2_label']          str     optional
    results['source']               str     'file' | 'api' | 'manual'

Presentation rules:

  - Numbers are right-aligned so columns can be scanned vertically.
  - Status is a soft pill (``BadgeDelegate``), not a filled cell.
  - With no data the six tabs are replaced by a single placeholder line; an
    empty tab strip at startup read as an unfinished screen.

Rendering limits: a run over thousands of files can produce a log with
hundreds of thousands of entries. Feeding all of it to a QPlainTextEdit
allocates a huge document and blocks the UI thread, so the log view is capped
at ``MAX_LOG_LINES`` with an explicit "truncated" banner. The full log is
always present in the exported report.
"""
import csv
from pathlib import Path
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QHeaderView, QLabel, QPlainTextEdit, QTabWidget,
    QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from .delegates import BadgeDelegate
from .theme import (
    DANGER,
    SUCCESS,
    TEXT_SECONDARY,
    WARNING,
    status_style,
)


DIFF_PREVIEW_LIMIT = 100
MAX_CELL_TEXT = 80
MAX_LOG_LINES = 20_000

TAB_SUMMARY = 0
TAB_COLUMN_RATES = 1
TAB_DEBUG_LOG = 2

RIGHT = Qt.AlignRight | Qt.AlignVCenter
LEFT = Qt.AlignLeft | Qt.AlignVCenter


def _bold(base: QFont) -> QFont:
    """Bold variant of an existing font.

    A ``QFont`` built with an empty family name leaves the family unset, and
    Qt resolves that to the legacy ``MS Sans Serif`` bitmap face. That face has
    no bold cut, so every paint logs::

        DirectWrite: CreateFontFaceFromHDC() failed ... Family="MS Sans Serif"

    Deriving the bold face from a real font keeps the family and pixel size
    the stylesheet already resolved.
    """
    font = QFont(base)
    font.setBold(True)
    return font


def _number_cell(text: str, alignment=RIGHT) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setTextAlignment(alignment)
    return item


class ResultsPanel(QWidget):
    """Renders merged validator results; exposes diff CSV paths for export."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ResultsPanel")
        self._results: Optional[dict] = None
        self._diff_csv_paths: List[Tuple[str, str]] = []
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.placeholder = QLabel(
            "No results yet — choose a mode above and start a run."
        )
        self.placeholder.setObjectName("ResultsPlaceholder")
        self.placeholder.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.placeholder)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("ResultsTabs")

        # Rich text so the key/value pairs align in a proportional font.
        # A monospaced QPlainTextEdit made the summary read like log output.
        self.summary_text = QTextBrowser()
        self.summary_text.setObjectName("SummaryText")
        self.summary_text.setOpenExternalLinks(False)
        self.tabs.addTab(self.summary_text, "Summary")

        self.column_table = QTableWidget(0, 8)
        self.column_table.setObjectName("ColumnTable")
        self.column_table.setHorizontalHeaderLabels([
            'Column', 'Status', 'Match Rate', 'Matches',
            'Mismatches', 'Total', 'Both Empty', 'One-side Empty',
        ])
        self._configure_table(self.column_table, stretch_column=0)
        self.column_table.setItemDelegateForColumn(1, BadgeDelegate(self.column_table))
        self.tabs.addTab(self.column_table, "Column Match Rates")

        # Log lines are fixed-width columns; wrapping destroys the alignment
        # and makes the timestamps impossible to scan.
        self.log_text = QPlainTextEdit()
        self.log_text.setObjectName("LogText")
        self.log_text.setReadOnly(True)
        self.log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.tabs.addTab(self.log_text, "Debug Log")

        self.sku_log_text = QPlainTextEdit()
        self.sku_log_text.setObjectName("SkuLogText")
        self.sku_log_text.setReadOnly(True)
        self.sku_log_text.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.tabs.addTab(self.sku_log_text, "SKU Detection")

        self.file_table = QTableWidget(0, 10)
        self.file_table.setObjectName("FileTable")
        self.file_table.setHorizontalHeaderLabels([
            'File', 'Pair ID', 'Status', 'SKU Col / Detail',
            'Rows F1', 'Rows F2', 'Common SKUs', 'Diffs', 'Miss F2', 'Miss F1',
        ])
        self._configure_table(self.file_table, stretch_column=0)
        self.tabs.addTab(self.file_table, "Files")

        self.diff_table = QTableWidget(0, 5)
        self.diff_table.setObjectName("DiffTable")
        self.diff_table.setHorizontalHeaderLabels(
            ['SKU', 'Column', 'Value F1', 'Value F2', 'Pair']
        )
        self._configure_table(self.diff_table, stretch_column=2)
        header = self.diff_table.horizontalHeader()
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.tabs.addTab(self.diff_table, "Differences")

        layout.addWidget(self.tabs)
        self.tabs.setCurrentIndex(TAB_SUMMARY)
        self.tabs.setVisible(False)

    @staticmethod
    def _configure_table(table: QTableWidget, stretch_column: int) -> None:
        """Size columns to their content and stretch the descriptive one."""
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(28)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.NoSelection)
        table.setWordWrap(False)
        table.setShowGrid(False)
        # Very light banding; dense numeric rows are hard to track otherwise.
        table.setAlternatingRowColors(True)
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)

    @staticmethod
    def _align_headers(table: QTableWidget, mapping) -> None:
        """Align header text to match its column's cell alignment.

        Qt centres header text by default, which left the "Column" heading
        floating over the middle of a stretched column while its values sat at
        the left edge.
        """
        for column, alignment in mapping.items():
            item = table.horizontalHeaderItem(column)
            if item is not None:
                item.setTextAlignment(alignment)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def render(self, results: dict) -> None:
        self._results = results
        self._diff_csv_paths = []
        self._render_summary(results)
        self._render_column_rates(results)
        self._render_logs(results)
        self._render_sku_log(results)
        self._render_files(results)
        self._render_diffs(results)
        self.placeholder.setVisible(False)
        self.tabs.setVisible(True)

    def clear(self) -> None:
        self._results = None
        self._diff_csv_paths = []
        self.summary_text.clear()
        self.log_text.clear()
        self.sku_log_text.clear()
        self.column_table.clearSpans()
        self.file_table.clearSpans()
        self.diff_table.clearSpans()
        self.column_table.setRowCount(0)
        self.file_table.setRowCount(0)
        self.diff_table.setRowCount(0)
        self.diff_table.setHorizontalHeaderLabels(
            ['SKU', 'Column', 'Value F1', 'Value F2', 'Pair']
        )
        self.tabs.setCurrentIndex(TAB_SUMMARY)
        self.tabs.setVisible(False)
        self.placeholder.setVisible(True)

    def show_render_error(self, traceback_text: str) -> None:
        """Surface a rendering failure instead of raising inside the handler."""
        self.placeholder.setVisible(False)
        self.tabs.setVisible(True)
        self.summary_text.setPlainText(
            "The run completed but the results could not be rendered.\n\n"
            "This is a UI bug. The Debug Log tab holds the traceback, and the "
            "exported report still contains the full data.\n\n"
            + traceback_text
        )
        self.log_text.setPlainText(traceback_text)
        self.tabs.setCurrentIndex(TAB_SUMMARY)

    def get_diff_paths(self) -> List[Tuple[str, str]]:
        """Return [(display_name, diff_csv_path), ...] for report export."""
        return list(self._diff_csv_paths)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _show_empty_message(table: QTableWidget, cols: int, message: str) -> None:
        """Render a single centred message row spanning all columns."""
        table.clearSpans()
        table.setRowCount(1)
        item = QTableWidgetItem(message)
        item.setTextAlignment(Qt.AlignCenter)
        item.setForeground(QColor(TEXT_SECONDARY))
        table.setSpan(0, 0, 1, cols)
        table.setItem(0, 0, item)

    # ------------------------------------------------------------------
    # Renderers
    # ------------------------------------------------------------------

    def _render_summary(self, results: dict) -> None:
        overall = results.get('overall', {})
        total_pairs = overall.get('total_pairs', 0)
        success = overall.get('success', 0)
        skipped = overall.get('skipped', 0)
        validation_failed = overall.get('validation_failed', overall.get('failed', 0))
        download_failed = overall.get('download_failed', 0)
        total_skus = overall.get('total_skus', 0)
        total_diffs = overall.get('total_diffs', 0)
        total_missing = overall.get('total_missing', 0)
        avg_rate = overall.get('avg_match_rate', 0)

        source = results.get('source', 'file')
        site1 = results.get('site1_label', '') or '—'
        site2 = results.get('site2_label', '') or '—'
        mode_label = {
            'file': 'File Mode',
            'api': 'API Mode',
            'manual': 'Manual URL Mode',
        }.get(source, source)

        def kv(label, value):
            return (
                '<tr>'
                '<td style="color:#667085; padding:3px 24px 3px 0;">{}</td>'
                '<td style="color:#1f2937; padding:3px 0;"><b>{}</b></td>'
                '</tr>'
            ).format(label, value)

        parts = [
            '<div style="font-size:13px;">',
            '<p style="color:#1f2937; font-size:15px; font-weight:600; '
            'margin:0 0 12px 0;">VALIDATION SUMMARY</p>',
            '<table cellspacing="0" cellpadding="0">',
            kv('Mode', mode_label),
        ]
        if source in ('api', 'manual'):
            parts.append(kv('Site 1', site1))
            parts.append(kv('Site 2', site2))
        parts.append('</table>')

        parts.append(
            '<p style="margin:14px 0 4px 0; color:#1f2937;">'
            f'{total_pairs} pairs · {success} compared · {skipped} skipped · '
            f'{download_failed} download failed · '
            f'{validation_failed} validation failed</p>'
        )

        parts.append('<table cellspacing="0" cellpadding="0" style="margin-top:10px;">')
        parts.append(kv('Common SKUs compared', f'{total_skus:,}'))
        parts.append(kv('Differences found', f'{total_diffs:,}'))
        parts.append(kv('Missing SKUs', f'{total_missing:,}'))
        parts.append(kv('Overall match rate', f'{avg_rate}%'))
        parts.append('</table>')

        parts.append(
            '<p style="margin-top:18px; color:#98a2b3; font-size:12px;">'
            'Column Match Rates has the per-column detail; Files has per-pair '
            'results and errors.</p>'
        )
        parts.append('</div>')
        self.summary_text.setHtml(''.join(parts))

    def _render_column_rates(self, results: dict) -> None:
        overall_stats = results.get('overall_column_stats', {})
        if not isinstance(overall_stats, dict):
            overall_stats = {}

        # Always clear previous spans; otherwise a stale span from an earlier
        # empty-result render collapses the first row of the next non-empty
        # render into a single cell.
        self.column_table.clearSpans()

        if not overall_stats:
            self._show_empty_message(
                self.column_table, 8,
                "No columns compared. All pairs may have failed to download "
                "or share no common columns."
            )
            return

        self._align_headers(self.column_table,
                            {0: LEFT, 1: Qt.AlignCenter, **{c: RIGHT for c in range(2, 8)}})

        sorted_cols = sorted(
            overall_stats.items(),
            key=lambda kv: kv[1].get('match_rate', 0),
        )
        self.column_table.setRowCount(len(sorted_cols))

        for row, (col, stats) in enumerate(sorted_cols):
            rate = stats.get('match_rate', 0)
            label, background, foreground = status_style(stats.get('status', 'BAD'))

            self.column_table.setItem(row, 0, QTableWidgetItem(col))

            badge = QTableWidgetItem(label)
            badge.setData(Qt.UserRole, (label, background, foreground))
            badge.setToolTip(f"{rate:.1f}% match")
            self.column_table.setItem(row, 1, badge)

            rate_item = _number_cell(f"{rate:.1f}%")
            rate_item.setForeground(QColor(foreground))
            rate_item.setFont(_bold(self.column_table.font()))
            self.column_table.setItem(row, 2, rate_item)

            self.column_table.setItem(row, 3, _number_cell(f"{stats.get('exact_matches', 0):,}"))
            self.column_table.setItem(row, 4, _number_cell(f"{stats.get('mismatches', 0):,}"))
            self.column_table.setItem(row, 5, _number_cell(f"{stats.get('total_compared', 0):,}"))
            self.column_table.setItem(row, 6, _number_cell(f"{stats.get('both_nan', 0):,}"))
            one_side = stats.get('only_file1_nan', 0) + stats.get('only_file2_nan', 0)
            self.column_table.setItem(row, 7, _number_cell(f"{one_side:,}"))

    def _render_logs(self, results: dict) -> None:
        lines: List[str] = []

        api_lines = results.get('api_log_lines') or []
        if api_lines:
            lines.extend(["=" * 110, "API LOG", "=" * 110, ""])
            lines.extend(api_lines)
            lines.append("")

        debug_logs = results.get('debug_logs') or []
        lines.extend(["=" * 110, "DEBUG LOG", "=" * 110, ""])

        shown = debug_logs
        truncated = False
        if len(debug_logs) > MAX_LOG_LINES:
            shown = debug_logs[:MAX_LOG_LINES]
            truncated = True

        for entry in shown:
            time_str = entry.get('time', '')
            category = entry.get('category', '')
            file_name = entry.get('file', '')
            message = entry.get('message', '')
            if len(file_name) > 38:
                file_name = file_name[:35] + '...'
            lines.append(f"{time_str:<14s} [{category:<6s}] {file_name:<40s} {message}")

        if truncated:
            lines.append("")
            lines.append(
                f"... {len(debug_logs) - MAX_LOG_LINES:,} more entries not shown "
                f"(view cap is {MAX_LOG_LINES:,}). The exported report contains "
                f"the complete log."
            )

        lines.append("")
        lines.append("=" * 110)
        log_summary = results.get('log_summary', {})
        if log_summary:
            lines.append(f"LOG SUMMARY: {log_summary}")
        lines.append("=" * 110)

        self.log_text.setPlainText('\n'.join(lines))

    def _render_sku_log(self, results: dict) -> None:
        sku_log = results.get('sku_detection_log', [])
        if sku_log:
            shown = sku_log[:MAX_LOG_LINES]
            lines = ["=" * 80, "SKU COLUMN DETECTION LOG", "=" * 80, ""] + list(shown)
            if len(sku_log) > MAX_LOG_LINES:
                lines.append(
                    f"... {len(sku_log) - MAX_LOG_LINES:,} more entries not shown."
                )
            self.sku_log_text.setPlainText('\n'.join(lines))
        else:
            self.sku_log_text.setPlainText("(no SKU detection entries)")

    def _render_files(self, results: dict) -> None:
        files = results.get('files', {})
        self.file_table.clearSpans()

        if not files:
            self._show_empty_message(
                self.file_table, 10,
                "No files were processed. This usually means every pair "
                "failed to download."
            )
            return

        self._align_headers(self.file_table,
                            {0: LEFT, 1: LEFT, **{c: RIGHT for c in range(4, 10)}})
        self.file_table.setRowCount(len(files))

        for row, (display_name, result) in enumerate(files.items()):
            name_item = QTableWidgetItem(display_name)
            name_item.setToolTip(display_name)
            self.file_table.setItem(row, 0, name_item)

            pair_item = QTableWidgetItem(result.get('pair_id', ''))
            pair_item.setForeground(QColor(TEXT_SECONDARY))
            self.file_table.setItem(row, 1, pair_item)

            status = result.get('status', 'error')
            if status == 'success':
                status_item = QTableWidgetItem('Compared')
                status_item.setForeground(QColor(SUCCESS))
                status_item.setFont(_bold(self.file_table.font()))
                self.file_table.setItem(row, 2, status_item)

                sku_col1 = result.get('sku_col1', '') or ''
                sku_col2 = result.get('sku_col2', '') or ''
                sku_info = sku_col1 if sku_col1 == sku_col2 else f"{sku_col1} / {sku_col2}"
                self.file_table.setItem(row, 3, QTableWidgetItem(sku_info))

                s = result.get('summary', {})
                self.file_table.setItem(row, 4, _number_cell(f"{s.get('rows_file1', 0):,}"))
                self.file_table.setItem(row, 5, _number_cell(f"{s.get('rows_file2', 0):,}"))
                self.file_table.setItem(row, 6, _number_cell(f"{s.get('common_skus', 0):,}"))

                diffs = s.get('total_differences', 0)
                diff_item = _number_cell(f"{diffs:,}")
                if diffs > 0:
                    diff_item.setForeground(QColor(DANGER))
                    diff_item.setFont(_bold(self.file_table.font()))
                self.file_table.setItem(row, 7, diff_item)

                self.file_table.setItem(row, 8, _number_cell(f"{s.get('only_in_file1', 0):,}"))
                self.file_table.setItem(row, 9, _number_cell(f"{s.get('only_in_file2', 0):,}"))

                diff_path = result.get('diff_csv_path')
                if diff_path and Path(diff_path).exists():
                    self._diff_csv_paths.append((display_name, diff_path))

            elif status == 'skipped':
                status_item = QTableWidgetItem('Skipped')
                status_item.setForeground(QColor(WARNING))
                self.file_table.setItem(row, 2, status_item)
                reason = result.get('reason', '')
                reason_item = QTableWidgetItem(reason[:MAX_CELL_TEXT])
                if len(reason) > MAX_CELL_TEXT:
                    reason_item.setToolTip(reason)
                self.file_table.setItem(row, 3, reason_item)

            elif status == 'download_failed':
                status_item = QTableWidgetItem('Download Failed')
                status_item.setForeground(QColor(DANGER))
                status_item.setFont(_bold(self.file_table.font()))
                self.file_table.setItem(row, 2, status_item)
                error = result.get('error', '')
                err_item = QTableWidgetItem(error[:MAX_CELL_TEXT])
                err_item.setToolTip(error)
                self.file_table.setItem(row, 3, err_item)

            else:  # 'error'
                status_item = QTableWidgetItem('Failed')
                status_item.setForeground(QColor(DANGER))
                status_item.setFont(_bold(self.file_table.font()))
                self.file_table.setItem(row, 2, status_item)
                error = result.get('error', '')
                err_item = QTableWidgetItem(error[:MAX_CELL_TEXT])
                err_item.setToolTip(error)
                self.file_table.setItem(row, 3, err_item)

    def _render_diffs(self, results: dict) -> None:
        site1_label = results.get('site1_label', '') or 'Value F1'
        site2_label = results.get('site2_label', '') or 'Value F2'
        self.diff_table.setHorizontalHeaderLabels(
            ['SKU', 'Column', site1_label, site2_label, 'Pair']
        )
        self.diff_table.clearSpans()

        preview_rows: List[List[str]] = []
        for display_name, diff_path in self._diff_csv_paths:
            if len(preview_rows) >= DIFF_PREVIEW_LIMIT:
                break
            try:
                with open(diff_path, 'r', encoding='utf-8-sig') as f:
                    reader = csv.reader(f)
                    next(reader, None)
                    for row in reader:
                        if len(preview_rows) >= DIFF_PREVIEW_LIMIT:
                            break
                        if len(row) < 4:
                            continue
                        preview_rows.append([row[0], row[1], row[2], row[3], display_name])
            except Exception:
                continue

        if not preview_rows:
            self._show_empty_message(
                self.diff_table, 5,
                "No cell-level differences to preview."
            )
            return

        self.diff_table.setRowCount(len(preview_rows))
        for row_idx, (sku, column, v1, v2, pair_name) in enumerate(preview_rows):
            self.diff_table.setItem(row_idx, 0, QTableWidgetItem(str(sku)))
            self.diff_table.setItem(row_idx, 1, QTableWidgetItem(column))

            item_v1 = QTableWidgetItem(str(v1))
            item_v2 = QTableWidgetItem(str(v2))
            if v1 != v2:
                item_v1.setBackground(QColor('#fdf2f0'))
                item_v2.setBackground(QColor('#eefaf2'))
            self.diff_table.setItem(row_idx, 2, item_v1)
            self.diff_table.setItem(row_idx, 3, item_v2)
            self.diff_table.setItem(row_idx, 4, QTableWidgetItem(pair_name))
