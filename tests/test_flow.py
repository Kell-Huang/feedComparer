"""End-to-end flow test: real QThread worker driven through MainWindow.

This is the closest thing to a user actually clicking Run. It exercises the
signal wiring, the worker's three phases, result rendering, auto-export and
the run-state reset — the paths that unit tests on core cannot reach.
"""

import os
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import make_test_data as gen
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication

DATA = Path(__file__).resolve().parent / "data"
RUN_TIMEOUT_MS = 120_000
_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


_app()


class _SilentDialog:
    """Stand-in for CompletionDialog so the run does not block on a modal."""

    def __init__(self, *args, **kwargs):
        self.summary = args[2] if len(args) > 2 else kwargs.get("summary_data", {})

    def exec(self):
        return 0


def _wait_for_run(window, payload):
    """Trigger a run and block until the worker emits a terminal signal."""
    loop = QEventLoop()
    window._on_run_requested(payload)
    worker = window._current_worker
    assert worker is not None, "run did not start"

    worker.results_ready.connect(loop.quit)
    worker.error.connect(loop.quit)
    worker.cancelled.connect(loop.quit)
    QTimer.singleShot(RUN_TIMEOUT_MS, loop.quit)
    loop.exec()
    # Flush the queued _on_results_ready / _on_error slot.
    _app().processEvents()


def test_file_mode_run_end_to_end():
    import ui.main_window as mw_mod

    if not (DATA / gen.SITE_A).exists():
        gen.build(DATA)

    original_dialog = mw_mod.CompletionDialog
    original_dir = mw_mod.AUTO_EXPORT_DIR
    report_dir = Path(tempfile.mkdtemp(prefix="feed_reports_"))
    mw_mod.CompletionDialog = _SilentDialog
    mw_mod.AUTO_EXPORT_DIR = report_dir

    window = mw_mod.MainWindow()
    try:
        window.file_tab.f1_edit.setText(str(DATA / gen.SITE_A))
        window.file_tab.f2_edit.setText(str(DATA / gen.SITE_B))
        payload = window.file_tab.build_run_payload()
        assert payload is not None

        _wait_for_run(window, payload)

        results = window._current_results
        assert results is not None, "no results were produced"
        overall = results["overall"]
        assert overall["total_pairs"] == 11, overall
        assert overall["success"] == 10, overall
        assert overall["failed"] == 0, overall
        assert overall["skipped"] == 1, overall

        # This run uses the shipped UI defaults, which exclude stock/price
        # columns, so it finds 5 differences instead of the 15 that the
        # core tests see with no exclusions:
        #   feed_main 1 (title), feed_broken 1, feed_numeric 1,
        #   feed_pipe 1, feed_semicolon 1
        assert window.file_tab.config_widget.exclude_edit.text() == "stock, price"
        assert overall["total_diffs"] == 5, overall

        # Run state restored.
        assert window._current_worker is None
        assert window.run_btn.isEnabled()
        assert window.export_btn.isEnabled()
        assert window.progress_bar.value() == 100
        assert "Done" in window.status_label.text()

        # Results rendered into the panel.
        assert window.results_panel.file_table.rowCount() == 11
        assert window.results_panel.column_table.rowCount() > 0
        assert window.results_panel.get_diff_paths()

        # File mode names both sides after their folders, so the results
        # tables say which folder a value came from instead of "Value F1".
        assert results["site1_label"] == gen.SITE_A, results["site1_label"]
        assert results["site2_label"] == gen.SITE_B, results["site2_label"]
        headers = [
            window.results_panel.diff_table.horizontalHeaderItem(i).text()
            for i in range(5)
        ]
        assert headers[2:4] == [gen.SITE_A, gen.SITE_B], headers
        # And the column-rate table can be narrowed to a single pair.
        assert window.results_panel.rate_scope_combo.count() > 1
        assert window.results_panel.rate_scope_combo.itemText(0) == "All pairs"

        # Auto-export produced a real archive.
        reports = list(report_dir.glob("*.zip"))
        assert len(reports) == 1, reports
        with zipfile.ZipFile(reports[0]) as zf:
            names = set(zf.namelist())
            assert "Summary.csv" in names, names
            assert "Files.csv" in names, names
            assert any(n.startswith("Differences_") for n in names), names
            summary = zf.read("Summary.csv").decode("utf-8-sig")
            assert "total_pairs" in summary
    finally:
        window.close()
        mw_mod.CompletionDialog = original_dialog
        mw_mod.AUTO_EXPORT_DIR = original_dir


def test_run_with_missing_folder_reports_error():
    """A bad path must surface as an error and leave the UI usable."""
    import ui.main_window as mw_mod

    original_dialog = mw_mod.CompletionDialog
    original_message = mw_mod.show_message
    messages = []
    mw_mod.CompletionDialog = _SilentDialog
    mw_mod.show_message = lambda parent, title, text, icon=None: messages.append(title)

    window = mw_mod.MainWindow()
    try:
        payload = {
            "mode": "file",
            "folder1": str(DATA / "does_not_exist"),
            "folder2": str(DATA / gen.SITE_B),
            "config": window.file_tab.config_widget.get_config(),
        }
        _wait_for_run(window, payload)

        assert window._current_worker is None
        assert window.run_btn.isEnabled()
        assert "Error" in messages, messages
        assert window.status_label.text().startswith("Error")
        assert window._current_results is None
    finally:
        window.close()
        mw_mod.CompletionDialog = original_dialog
        mw_mod.show_message = original_message
