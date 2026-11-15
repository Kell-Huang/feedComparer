"""Main application window.

Owns the three mode tabs, the results panel, and the current worker. All
worker construction, signal wiring, export, and cleanup lives here so the mode
tabs stay purely about their own UI.

Shutdown contract:

  - closeEvent calls worker.request_stop() and waits for the worker to exit
    cooperatively. terminate() is never used because it can leave half-written
    CSV files on disk.
  - If the wait times out twice, the user is asked whether to force-exit;
    even force-exit does not terminate() the thread — it simply lets Python
    and Qt clean up on app exit while the worker finishes its current step.

Run-state contract:

  - Exactly one terminal signal is delivered per run (``results_ready``,
    ``error`` or ``cancelled``). Every handler resets the running state, so a
    cancelled run can never leave the UI stuck in "Running...".
"""
import csv
import json
import re
import shutil
import tempfile
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)

from core.models import PairSource
from core.progress import truncate

from .api_mode_tab import ApiModeTab
from .dialogs import CompletionDialog, show_message
from .file_mode_tab import FileModeTab
from .icons import make_icon
from .theme import app_icon
from .manual_url_tab import ManualUrlTab
from .results_panel import ResultsPanel
from .workers import DownloadAndCompareWorker, ValidationWorker


APP_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_ROOT / "platform_config.json"

# Auto-exported reports live next to the application, not in whatever
# directory happened to be the process working directory at launch.
AUTO_EXPORT_DIR = APP_ROOT / "reports"

MERGE_DIFF_FILES = False
# Initial value for the "Exclude Columns" field. This is a UI-editable
# default: the user can change it per run from the configuration panel.
EXCLUDE_COLUMNS_KEYWORDS = ['stock', 'price']

# Total cooperative shutdown budget: 10s + 10s = 20s before prompting the user.
SHUTDOWN_WAIT_MS = 10000

_EXPORT_COPY_CHUNK = 1 << 20


def _load_config() -> dict:
    default = {
        "auth_token": "",
        "base_url": "https://platform-api.productsup.io/platform/v2",
        # Secure by default. Self-signed endpoints must opt out explicitly
        # in platform_config.json, and the app will warn once at startup.
        "verify_ssl": True,
        "download_timeout": 120,
    }
    try:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    return {**default, **loaded}
    except Exception as e:
        import sys
        sys.stderr.write(f"Warning: failed to read {CONFIG_PATH.name}: {e}\n")
    return default


def _safe_filename(name: str) -> str:
    """Strip characters that are illegal in file names, and collapse spaces."""
    cleaned = re.sub(r'[<>:"/\\|?*]', '_', name)
    cleaned = re.sub(r'\s+', '_', cleaned).strip('_')
    return cleaned or 'unnamed'


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._config = _load_config()
        self.setWindowIcon(app_icon())
        self._current_worker = None
        self._current_results: Optional[dict] = None
        self._log_lines: List[str] = []
        self._closing = False
        self._build_ui()
        self._wire_signals()
        self._warn_if_insecure()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self.setWindowTitle('Feed Validator')
        self.setMinimumSize(1100, 720)
        self.resize(1500, 1000)
        self.setObjectName("MainWindow")

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Vertical)
        splitter.setObjectName("MainSplitter")
        # Collapsible so the user can reclaim the space when the results panel
        # is not interesting, but never below the usable minimums below.
        splitter.setChildrenCollapsible(True)
        self.splitter = splitter

        self.mode_tabs = QTabWidget()
        self.mode_tabs.setObjectName("ModeTabs")
        # The mode forms are taller than a laptop window; this floor keeps the
        # scroll area usable instead of collapsing it to a sliver.
        self.mode_tabs.setMinimumHeight(330)

        self.file_tab = FileModeTab(exclude_keywords=EXCLUDE_COLUMNS_KEYWORDS)
        self.api_tab = ApiModeTab(
            base_url=self._config['base_url'],
            headers={"X-Auth-Token": self._config['auth_token']},
            verify_ssl=self._config.get('verify_ssl', True),
            exclude_keywords=EXCLUDE_COLUMNS_KEYWORDS,
        )
        self.manual_tab = ManualUrlTab(exclude_keywords=EXCLUDE_COLUMNS_KEYWORDS)
        self.mode_tabs.addTab(self.file_tab, "File Mode")
        self.mode_tabs.addTab(self.api_tab, "API Mode")
        self.mode_tabs.addTab(self.manual_tab, "Manual URL")

        # Action bar lives outside the scrolling tab content so the primary
        # button is always reachable. Inside the tab it ended up below the
        # fold on a 720p display: the user had to scroll to start a run.
        top = QWidget()
        top.setObjectName("TopSection")
        top_layout = QVBoxLayout(top)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(0)
        top_layout.addWidget(self.mode_tabs, stretch=1)
        top_layout.addWidget(self._build_action_bar())
        splitter.addWidget(top)

        self.results_panel = ResultsPanel()
        self.results_panel.setMinimumHeight(64)
        splitter.addWidget(self.results_panel)

        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        # Start with the results panel small. Before a run it is entirely
        # empty, and giving it 40% of the window clipped the mode tabs' own
        # controls on a 720p display.
        splitter.setSizes([660, 64])
        outer.addWidget(splitter, stretch=1)

        bottom = QWidget()
        bottom.setObjectName("BottomBar")
        bottom_layout = QVBoxLayout(bottom)
        bottom_layout.setContentsMargins(12, 6, 12, 6)
        bottom_layout.setSpacing(4)
        self.progress_bar = QProgressBar()
        self.progress_bar.setObjectName("ProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        bottom_layout.addWidget(self.progress_bar)
        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("StatusLabel")
        bottom_layout.addWidget(self.status_label)
        outer.addWidget(bottom)

    def _build_action_bar(self) -> QWidget:
        """Persistent Run / Export / Clear bar shared by all three modes."""
        bar = QWidget()
        bar.setObjectName("ActionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        self.run_btn = QPushButton("Start Validation")
        self.run_btn.setObjectName("runBtn")
        self.run_btn.setIcon(make_icon('play', '#ffffff'))
        self.run_btn.setMinimumWidth(180)
        self.run_btn.clicked.connect(self._on_run_clicked)
        layout.addWidget(self.run_btn)

        self.export_btn = QPushButton("Export Report")
        self.export_btn.setObjectName("exportBtn")
        self.export_btn.setIcon(make_icon('download', '#344054'))
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export_requested)
        layout.addWidget(self.export_btn)

        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("clearBtn")
        self.clear_btn.setIcon(make_icon('trash', '#344054'))
        self.clear_btn.clicked.connect(self._on_clear_requested)
        layout.addWidget(self.clear_btn)

        # Only visible while a run is in flight. Aborting used to require
        # closing the window, which is far too blunt for a slow download.
        self.cancel_btn = QPushButton("Cancel Run")
        self.cancel_btn.setObjectName("cancelRunBtn")
        self.cancel_btn.setIcon(make_icon('stop', '#b42318'))
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._on_cancel_clicked)
        layout.addWidget(self.cancel_btn)

        layout.addStretch()
        return bar

    def _on_cancel_clicked(self) -> None:
        if self._current_worker is None:
            return
        self._current_worker.request_stop()
        self.cancel_btn.setEnabled(False)
        self.status_label.setText("Cancelling — waiting for in-flight requests to abort…")

    def _current_tab(self):
        return self.mode_tabs.currentWidget()

    def _on_run_clicked(self) -> None:
        tab = self._current_tab()
        build = getattr(tab, 'build_run_payload', None)
        if build is None:
            return
        payload = build()
        if payload:
            self._on_run_requested(payload)

    def _update_run_button_label(self, index: int) -> None:
        if index == 0:
            self.run_btn.setText("Start Validation")
            self.run_btn.setToolTip("Compare the two selected folders")
        else:
            # "&&" renders as a literal "&"; a single one is eaten as a
            # keyboard mnemonic marker and shows as "Download _Compare".
            self.run_btn.setText("Download && Compare")
            self.run_btn.setToolTip("Download every pair, then compare them")

    def _wire_signals(self) -> None:
        self.api_tab.log.connect(self._on_api_log)
        self.mode_tabs.currentChanged.connect(self._update_run_button_label)

    def _warn_if_insecure(self) -> None:
        """One-time startup warning when TLS verification is disabled."""
        if self._config.get('verify_ssl', True):
            return
        show_message(
            self, "Insecure TLS",
            "SSL certificate verification is DISABLED in platform_config.json.\n\n"
            "Downloads will accept self-signed certificates, which exposes "
            "you to man-in-the-middle attacks. Only do this for trusted "
            "internal services.\n\n"
            "Set \"verify_ssl\": true in platform_config.json to re-enable "
            "verification.",
            QMessageBox.Warning,
        )

    # ------------------------------------------------------------------
    # Run dispatch
    # ------------------------------------------------------------------

    def _on_run_requested(self, payload: dict) -> None:
        mode = payload.get('mode', '')
        config = payload.get('config')
        if config is None:
            show_message(
                self, "Invalid Request",
                "Run request is missing 'config'. This is a UI bug.",
                QMessageBox.Critical,
            )
            return

        if mode == 'file':
            if 'folder1' not in payload or 'folder2' not in payload:
                show_message(
                    self, "Invalid Request",
                    "File mode requires 'folder1' and 'folder2'.",
                    QMessageBox.Critical,
                )
                return
            self._start_file_validation(payload)
        elif mode in ('api', 'manual'):
            if 'pairs' not in payload:
                show_message(
                    self, "Invalid Request",
                    f"{mode} mode requires 'pairs'.",
                    QMessageBox.Critical,
                )
                return
            self._start_download_compare(payload)
        else:
            show_message(
                self, "Unknown Mode",
                f"Unsupported mode: {mode}", QMessageBox.Critical,
            )

    def _attach_worker(self, worker) -> None:
        """Wire the shared signals and start the worker."""
        worker.progress.connect(self._on_progress)
        if hasattr(worker, 'pair_status'):
            worker.pair_status.connect(self._on_pair_status)
        worker.results_ready.connect(self._on_results_ready)
        worker.error.connect(self._on_error)
        worker.cancelled.connect(self._on_cancelled)
        # Let Qt own the lifetime of the thread object once it has exited.
        worker.finished.connect(worker.deleteLater)
        self._current_worker = worker
        self._set_all_running(True)
        worker.start()

    def _start_file_validation(self, payload: dict) -> None:
        self._reset_for_run()
        worker = ValidationWorker(
            payload['folder1'], payload['folder2'], payload['config'],
        )
        self._attach_worker(worker)

    def _start_download_compare(self, payload: dict) -> None:
        config = payload['config']
        mode = payload['mode']
        source = PairSource.API if mode == 'api' else PairSource.MANUAL

        # Only API mode sends platform credentials. Manual URLs are user-supplied
        # and may point to third-party servers that must not receive the token.
        headers = {"X-Auth-Token": self._config['auth_token']} if mode == 'api' else {}
        verify_ssl = self._config.get('verify_ssl', True)

        self._reset_for_run()
        worker = DownloadAndCompareWorker(
            pairs=payload['pairs'],
            site1_label=payload.get('site1_label', ''),
            site2_label=payload.get('site2_label', ''),
            source=source,
            headers=headers,
            verify_ssl=verify_ssl,
            sku_col=config.get('sku_column'),
            compare_cols=config.get('compare_columns') or [],
            normalize_sku=config.get('normalize_sku', True),
            case_sensitive=config.get('case_sensitive', True),
            debug=config.get('debug', True),
            auto_detect=config.get('auto_detect', True),
            priority_list=config.get('priority_list'),
            exclude_keywords=config.get('exclude_keywords'),
            download_workers=config.get('workers', 4),
            validate_workers=config.get('workers', 4),
            download_timeout=int(self._config.get('download_timeout', 120)),
        )
        worker.log.connect(self._on_api_log)
        self._attach_worker(worker)

    def _set_results_expanded(self, expanded: bool) -> None:
        """Grow the results panel when there is something to show.

        Uses the splitter's current height so the ratio holds at any window
        size; falls back to the window height before the first layout pass.
        """
        total = self.splitter.height() or self.height() or 720
        if expanded:
            top = int(total * 0.55)
            self.splitter.setSizes([top, total - top])
        else:
            self.splitter.setSizes([max(total - 150, 360), 150])

    def _reset_for_run(self) -> None:
        # Release any previous run's disk resources before starting a new one.
        self._release_resources()
        self.results_panel.clear()
        self._on_enable_export(False)
        self._set_results_expanded(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Running...")

    # ------------------------------------------------------------------
    # Worker callbacks
    # ------------------------------------------------------------------

    def _on_progress(self, update) -> None:
        """Render a ProgressUpdate: monotonic bar plus a single status line."""
        if self._closing:
            return
        self.progress_bar.setValue(int(round(update.overall * 100)))
        label = truncate(update.label, 64)
        text = f"[{update.index}/{update.total}] {label}" if update.total else label
        if update.detail:
            text = f"{text}  ·  {update.detail}"
        self.status_label.setText(text[:180])

    def _on_pair_status(self, status) -> None:
        """Forward a per-pair download status to every tab that shows pairs.

        All of them, not just the visible one: switching tabs mid-run would
        otherwise leave the hidden table stuck on stale rows.
        """
        if self._closing:
            return
        for tab in (self.file_tab, self.api_tab, self.manual_tab):
            handler = getattr(tab, 'update_pair_status', None)
            if handler is not None:
                handler(status)

    def _finish_run(self) -> None:
        """Common teardown for every terminal worker signal."""
        self._current_worker = None
        self._set_all_running(False)

    def _on_results_ready(self, results: dict) -> None:
        if self._closing:
            return
        self._finish_run()

        results['api_log_lines'] = list(self._log_lines)
        self._current_results = results
        self._on_enable_export(True)

        render_ok = True
        try:
            self.results_panel.render(results)
        except Exception:
            render_ok = False
            self.results_panel.show_render_error(traceback.format_exc())

        if not render_ok:
            self.progress_bar.setValue(0)
            self.status_label.setText("Render failed (see Debug Log)")
            show_message(
                self, "Render Error",
                "Results were computed but the UI failed to display them.\n\n"
                "Details are in the Debug Log tab. Report export still works "
                "and includes the full data.",
                QMessageBox.Warning,
            )
            return

        self.progress_bar.setValue(100)
        self.status_label.setText("Done")
        self._set_results_expanded(True)
        report_path = self._auto_export_report()
        self._show_completion_dialog(results, report_path)

    def _on_error(self, message: str) -> None:
        if self._closing:
            return
        self._finish_run()
        first_line = (message or "Unknown error").split('\n')[0]
        self.status_label.setText(f"Error: {first_line[:100]}")
        self.progress_bar.setValue(0)
        show_message(self, "Error", first_line[:500], QMessageBox.Critical)

    def _on_cancelled(self) -> None:
        if self._closing:
            return
        self._finish_run()
        self.progress_bar.setValue(0)
        self.status_label.setText("Cancelled")
        self._release_resources()

    def _on_api_log(self, message: str) -> None:
        if self._closing:
            return
        self._log_lines.append(message)
        # While a run is in flight the progress line owns the status bar.
        # Letting log lines write here too made the two overwrite each other
        # on every message, so the progress text kept flickering away.
        if self._current_worker is None:
            self.status_label.setText(message[:120])

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    def _set_all_running(self, running: bool) -> None:
        for tab in (self.file_tab, self.api_tab, self.manual_tab):
            tab.set_running(running)
        self.run_btn.setEnabled(not running)
        self.clear_btn.setEnabled(not running)
        self.cancel_btn.setVisible(running)
        self.cancel_btn.setEnabled(running)

    def _on_enable_export(self, enabled: bool) -> None:
        self.export_btn.setEnabled(enabled)

    def _show_completion_dialog(self, results: dict, report_path: str = '') -> None:
        overall = results.get('overall', {})
        summary_data = {
            'total_pairs': overall.get('total_pairs', 0),
            'files': overall.get('success', 0),
            'skipped': overall.get('skipped', 0),
            'validation_failed': overall.get('validation_failed', overall.get('failed', 0)),
            'download_failed': overall.get('download_failed', 0),
            'match_rate': overall.get('avg_match_rate', 0),
            'diffs': overall.get('total_diffs', 0),
            'report_path': report_path,
        }
        try:
            CompletionDialog(self, 'Validation Complete', summary_data).exec()
        except Exception:
            pass

    def _on_clear_requested(self) -> None:
        if self._current_worker is not None:
            show_message(
                self, "Run in Progress",
                "A validation is currently running. Please wait for it to "
                "complete, or close the window to abort.",
                QMessageBox.Warning,
            )
            return
        self._release_resources()
        self.results_panel.clear()
        self._on_enable_export(False)
        self._set_results_expanded(False)
        self.progress_bar.setValue(0)
        self.status_label.setText("Ready")

    def _release_resources(self) -> None:
        """Invoke the cleanup closure attached to the current results."""
        if self._current_results:
            cleanup = self._current_results.get('cleanup')
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    pass
        self._current_results = None
        self._log_lines = []

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def _on_export_requested(self) -> None:
        if not self._current_results:
            show_message(
                self, "No Data", "No results to export.", QMessageBox.Warning,
            )
            return

        results = self._current_results
        site1 = results.get('site1_label', '') or ''
        site2 = results.get('site2_label', '') or ''
        if site1 and site2:
            default_name = (
                f"validation_{_safe_filename(site1)}_vs_"
                f"{_safe_filename(site2)}_{datetime.now():%Y%m%d_%H%M%S}.zip"
            )
        else:
            default_name = f"validation_{datetime.now():%Y%m%d_%H%M%S}.zip"

        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Report', default_name, 'ZIP Archive (*.zip)',
        )
        if not path:
            return
        try:
            self._export_to_zip(path, results)
            show_message(
                self, "Success", f"Report saved to:\n{path}", QMessageBox.Information,
            )
        except Exception as e:
            show_message(
                self, "Export Error",
                f"{e}\n\n{traceback.format_exc()[:500]}",
                QMessageBox.Critical,
            )

    def _auto_export_report(self) -> str:
        """Write the run report to ``reports/``. Returns the path, or ""."""
        if not self._current_results:
            return ""
        try:
            AUTO_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            results = self._current_results
            site1 = results.get('site1_label', '') or ''
            site2 = results.get('site2_label', '') or ''
            if site1 and site2:
                suffix = f"_{_safe_filename(site1)}_vs_{_safe_filename(site2)}"
            else:
                suffix = ""
            zip_path = AUTO_EXPORT_DIR / (
                f"validation{suffix}_{datetime.now():%Y%m%d_%H%M%S}.zip"
            )
            self._export_to_zip(str(zip_path), results)
            self.status_label.setText(f"Done — exported {zip_path.name}")
            return str(zip_path)
        except Exception as e:
            self.status_label.setText(f"Done (auto-export failed: {str(e)[:60]})")
            return ""

    @staticmethod
    def _write_csv(path: Path, fieldnames, rows) -> None:
        """Write a small CSV with a fixed column order (stdlib, no pandas)."""
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

    def _export_to_zip(self, zip_path: str, results: dict) -> None:
        site1 = results.get('site1_label', '') or ''
        site2 = results.get('site2_label', '') or ''
        diff_paths = self.results_panel.get_diff_paths()

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            overall = results.get('overall', {})
            if overall:
                self._write_csv(tmp / 'Summary.csv', list(overall.keys()), [overall])

            col_stats = results.get('overall_column_stats', {})
            if isinstance(col_stats, dict) and col_stats:
                rows = [
                    {
                        'Column': col,
                        'Status': s.get('status', ''),
                        'Match_Rate_%': s.get('match_rate', 0),
                        'Matches': s.get('exact_matches', 0),
                        'Mismatches': s.get('mismatches', 0),
                        'Total': s.get('total_compared', 0),
                        'Both_Empty': s.get('both_nan', 0),
                        'One_Side_Empty': s.get('only_file1_nan', 0) + s.get('only_file2_nan', 0),
                    }
                    for col, s in col_stats.items()
                ]
                rows.sort(key=lambda r: r['Match_Rate_%'])
                self._write_csv(
                    tmp / 'Column_Match_Rates.csv',
                    ['Column', 'Status', 'Match_Rate_%', 'Matches', 'Mismatches',
                     'Total', 'Both_Empty', 'One_Side_Empty'],
                    rows,
                )

            files = results.get('files', {})
            if files:
                file_rows = []
                for display_name, r in files.items():
                    summary = r.get('summary', {}) or {}
                    file_rows.append({
                        'File': display_name,
                        'Pair_ID': r.get('pair_id', ''),
                        'Status': r.get('status', ''),
                        'SKU_Col_F1': r.get('sku_col1', ''),
                        'SKU_Col_F2': r.get('sku_col2', ''),
                        'Rows_F1': summary.get('rows_file1', ''),
                        'Rows_F2': summary.get('rows_file2', ''),
                        'Common_SKUs': summary.get('common_skus', ''),
                        'Only_In_F1': summary.get('only_in_file1', ''),
                        'Only_In_F2': summary.get('only_in_file2', ''),
                        'Total_Differences': summary.get('total_differences', ''),
                        'Avg_Match_Rate': summary.get('avg_match_rate', ''),
                        'Error': r.get('error', ''),
                    })
                self._write_csv(
                    tmp / 'Files.csv',
                    ['File', 'Pair_ID', 'Status', 'SKU_Col_F1', 'SKU_Col_F2',
                     'Rows_F1', 'Rows_F2', 'Common_SKUs', 'Only_In_F1', 'Only_In_F2',
                     'Total_Differences', 'Avg_Match_Rate', 'Error'],
                    file_rows,
                )

            debug_logs = results.get('debug_logs', [])
            if debug_logs:
                self._write_csv(
                    tmp / 'Debug_Log.csv',
                    ['time', 'category', 'file', 'message'],
                    debug_logs,
                )

            api_log_lines = results.get('api_log_lines', []) or self._log_lines
            if api_log_lines:
                self._write_csv(
                    tmp / 'API_Log.csv', ['Log'],
                    [{'Log': line} for line in api_log_lines],
                )

            if diff_paths:
                if MERGE_DIFF_FILES:
                    self._write_merged_diff(tmp, diff_paths, site1, site2)
                else:
                    for display_name, diff_path in diff_paths:
                        if not Path(diff_path).exists():
                            continue
                        safe = _safe_filename(display_name)
                        self._copy_diff_streaming(
                            Path(diff_path), tmp / f'Differences_{safe}.csv',
                            site1, site2,
                        )

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for csv_file in sorted(tmp.glob('*.csv')):
                    zf.write(csv_file, csv_file.name)

    @staticmethod
    def _copy_diff_streaming(src: Path, dst: Path, site1: str, site2: str) -> None:
        """Rewrite a diff CSV's header and stream the body to ``dst``.

        A diff file can hold millions of rows; reading it into a DataFrame
        just to rename two columns would triple peak memory for no reason.
        """
        with open(src, 'r', encoding='utf-8-sig', newline='') as f_in, \
                open(dst, 'w', encoding='utf-8-sig', newline='') as f_out:
            f_in.readline()          # discard original header
            writer = csv.writer(f_out)
            if site1 and site2:
                writer.writerow(['sku', 'column', site1, site2])
            else:
                writer.writerow(['sku', 'column', 'Value_F1', 'Value_F2'])
            shutil.copyfileobj(f_in, f_out, _EXPORT_COPY_CHUNK)

    def _write_merged_diff(self, tmp: Path, diff_paths, site1: str, site2: str) -> None:
        """Merge per-pair diff CSVs into one file, streaming rows."""
        with open(tmp / 'Differences.csv', 'w', encoding='utf-8-sig', newline='') as f_out:
            writer = csv.writer(f_out)
            if site1 and site2:
                writer.writerow(['sku', 'column', site1, site2, 'Pair'])
            else:
                writer.writerow(['sku', 'column', 'Value_F1', 'Value_F2', 'Pair'])
            for display_name, diff_path in diff_paths:
                if not Path(diff_path).exists():
                    continue
                with open(diff_path, 'r', encoding='utf-8-sig', newline='') as f_in:
                    reader = csv.reader(f_in)
                    next(reader, None)
                    for row in reader:
                        if len(row) < 4:
                            continue
                        writer.writerow([row[0], row[1], row[2], row[3], display_name])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        """Cooperative shutdown. Never calls terminate()."""
        if self._current_worker is not None:
            reply = QMessageBox.question(
                self, 'Confirm Exit',
                'A validation is still running. Request it to stop and exit?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return

            self._closing = True
            self._current_worker.request_stop()

            if self._current_worker.wait(SHUTDOWN_WAIT_MS):
                self._current_worker = None
                self._release_resources()
                event.accept()
                return

            # Second chance. Worker is likely mid-write on a large file.
            if self._current_worker.wait(SHUTDOWN_WAIT_MS):
                self._current_worker = None
                self._release_resources()
                event.accept()
                return

            # Still running. Let the user decide whether to force-close.
            # We will NOT terminate(): the worker keeps its cancel_event set
            # and will exit at the next checkpoint. Qt cleans up on app exit.
            force = QMessageBox.question(
                self, 'Force Exit',
                'The background task is not stopping yet. Force exit?\n\n'
                'Any files it is currently writing may be incomplete. '
                'Temporary directories will still be cleaned when it exits.',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if force != QMessageBox.Yes:
                self._closing = False
                event.ignore()
                return

            self._release_resources()
            event.accept()
            return

        self._release_resources()
        event.accept()
