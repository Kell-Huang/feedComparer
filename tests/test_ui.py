"""UI tests running against the offscreen Qt platform.

These cover the wiring that cannot be checked by importing modules: the
results panel actually populating, the export producing the expected archive
members, and the run-state being restored after cancellation.
"""

import atexit
import os
import shutil
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import make_test_data as gen
from PySide6.QtWidgets import QApplication, QScrollArea

from core.validator import FastBatchValidator

DATA = Path(__file__).resolve().parent / "data"
_APP = None


def _app():
    global _APP
    if _APP is None:
        _APP = QApplication.instance() or QApplication([])
    return _APP


_TEMP_ROOTS = []


def _temp_root() -> Path:
    """A diff directory that outlives the validator for the whole test run.

    The panel and the exporter both read the diff CSVs back off disk, so
    calling validator.cleanup() inside the helper would delete them before
    they are used.
    """
    root = Path(tempfile.mkdtemp(prefix="feed_ui_test_"))
    _TEMP_ROOTS.append(root)
    atexit.register(shutil.rmtree, root, True)
    return root


def _results():
    _app()
    if not (DATA / gen.SITE_A).exists():
        gen.build(DATA)
    validator = FastBatchValidator(
        str(DATA / gen.SITE_A),
        str(DATA / gen.SITE_B),
        None,
        [],
        exclude_keywords=[],
        temp_diff_dir=_temp_root(),
    )
    results = validator.validate_all_parallel()
    results["cleanup"] = validator.cleanup
    results["site1_label"] = ""
    results["site2_label"] = ""
    results["source"] = "file"
    results["api_log_lines"] = ["log line 1", "log line 2"]
    return results


def test_main_window_builds():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.mode_tabs.count() == 4
        assert window.mode_tabs.tabText(window.settings_tab_index) == "Settings"
        assert window.minimumWidth() >= 1000
        assert window.results_panel.tabs.count() == 6
    finally:
        window.close()


def test_mode_tabs_are_scrollable():
    """A short window must scroll, not squeeze the tables to nothing."""
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        for tab in (window.file_tab, window.api_tab, window.manual_tab):
            assert tab.findChild(QScrollArea) is not None, tab.objectName()
        assert window.api_tab.pair_table.minimumHeight() >= 100
        assert window.api_tab.channel_list1.minimumHeight() >= 100
        assert window.manual_tab.url_left_edit.minimumHeight() >= 80
    finally:
        window.close()


def test_config_widget_exposes_exclude_keywords():
    from ui.config_widget import ComparisonConfigWidget

    _app()
    widget = ComparisonConfigWidget(exclude_keywords=["stock", "price"])
    config = widget.get_config()
    assert config["exclude_keywords"] == ["stock", "price"], config
    assert config["workers"] == 4
    assert config["auto_detect"] is True

    # The field is user-editable and drives the run.
    widget.exclude_edit.setText("promo, sale")
    assert widget.get_config()["exclude_keywords"] == ["promo", "sale"]

    widget.exclude_edit.setText("")
    assert widget.get_config()["exclude_keywords"] == []


def test_results_panel_renders_all_tabs():
    from ui.results_panel import ResultsPanel

    _app()
    results = _results()
    panel = ResultsPanel()
    panel.render(results)

    assert panel.column_table.rowCount() > 0
    assert panel.file_table.rowCount() == len(results["files"])
    assert panel.diff_table.rowCount() > 0
    assert "VALIDATION SUMMARY" in panel.summary_text.toPlainText()
    assert "DEBUG LOG" in panel.log_text.toPlainText()
    assert panel.get_diff_paths(), "diff CSVs should be discoverable"


def test_results_panel_handles_empty_results():
    from ui.results_panel import ResultsPanel

    _app()
    panel = ResultsPanel()
    panel.render(
        {
            "files": {},
            "overall": {},
            "overall_column_stats": {},
            "debug_logs": [],
            "sku_detection_log": [],
            "source": "file",
        }
    )
    assert panel.column_table.rowCount() == 1  # single message row
    assert panel.file_table.rowCount() == 1
    assert panel.diff_table.rowCount() == 1
    # Re-rendering real data must clear the spans from the empty state.
    panel.render(_results())
    assert panel.file_table.rowCount() > 1


def test_results_panel_show_render_error_exists():
    """MainWindow calls this on a render failure; it used to not exist."""
    from ui.results_panel import ResultsPanel

    _app()
    panel = ResultsPanel()
    panel.show_render_error("Traceback: boom")
    assert "boom" in panel.summary_text.toPlainText()


def test_export_writes_expected_archive_members():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        results = _results()
        window._current_results = results
        window.results_panel.render(results)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "report.zip"
            window._export_to_zip(str(target), results)

            assert target.exists()
            with zipfile.ZipFile(target) as zf:
                names = set(zf.namelist())
                assert "Summary.csv" in names, names
                assert "Files.csv" in names, names
                assert "Column_Match_Rates.csv" in names, names
                assert "Debug_Log.csv" in names, names
                assert any(n.startswith("Differences_") for n in names), names

                diff_name = next(n for n in names if n.startswith("Differences_"))
                header = zf.read(diff_name).decode("utf-8-sig").splitlines()[0]
                assert header == "sku,column,Value_F1,Value_F2", header

                summary = zf.read("Summary.csv").decode("utf-8-sig")
                assert "total_pairs" in summary
    finally:
        window.close()


def test_cancel_restores_ui_state():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window._set_all_running(True)
        assert not window.run_btn.isEnabled()

        window._on_cancelled()

        assert window.run_btn.isEnabled()
        assert window.clear_btn.isEnabled()
        assert "Cancelled" in window.status_label.text()
        assert window._current_worker is None
    finally:
        window.close()


def test_config_lives_on_the_settings_tab():
    """One shared config block, on its own tab, out of every mode form."""
    _app()
    from PySide6.QtWidgets import QGroupBox

    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        # No mode tab carries its own copy any more.
        for tab in (window.file_tab, window.api_tab, window.manual_tab):
            assert tab.findChild(QGroupBox, "configGroup") is None, tab.objectName()
        # The file filter is part of the shared config now, so it is always
        # shown rather than toggling as the user switches modes.
        assert not window.settings_widget.filter_combo.isHidden()
        assert window._settings_page.findChild(QGroupBox, "configGroup") is not None
    finally:
        window.close()


def test_settings_tab_cannot_start_a_run():
    """Run is disabled on Settings and the summary says what the config does."""
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.run_btn.isEnabled()
        window.mode_tabs.setCurrentIndex(window.settings_tab_index)
        assert not window.run_btn.isEnabled()
        assert not window.edit_config_btn.isEnabled()
        assert "SKU:" in window.config_summary.text()

        # Back on a mode tab it is usable again, and Edit is redundant there.
        window.mode_tabs.setCurrentIndex(0)
        assert window.run_btn.isEnabled()
        assert window.edit_config_btn.isEnabled()

        # The summary tracks the live widget, not a stale snapshot.
        window.settings_widget.exclude_edit.setText("promo")
        window.mode_tabs.setCurrentIndex(1)
        assert "promo" in window.config_summary.text()
    finally:
        window.close()


def test_action_bar_always_visible_and_wired():
    """Run/Export/Clear live outside the scrolling tabs and drive the right tab."""
    _app()
    import ui.api_mode_tab as api_mod
    import ui.file_mode_tab as file_mod
    import ui.main_window as mw_mod
    import ui.manual_url_tab as manual_mod

    stubs = (mw_mod, file_mod, api_mod, manual_mod)
    originals = [mod.show_message for mod in stubs]
    for mod in stubs:
        mod.show_message = lambda *args, **kwargs: 0
    try:
        window = mw_mod.MainWindow()
        try:
            # The action bar is a sibling of the tab widget, not inside a
            # scroll area, so it cannot scroll out of view.
            assert window.run_btn.parent() is not None
            assert not window.run_btn.parent().findChild(QScrollArea)

            # Invalid form -> no payload, no crash.
            config = window.settings_widget.get_config()
            assert window.file_tab.build_run_payload(config) is None

            window.file_tab.f1_edit.setText("tests/data/site_a")
            window.file_tab.f2_edit.setText("tests/data/site_b")
            payload = window.file_tab.build_run_payload(config)
            assert payload["mode"] == "file"
            assert payload["config"]["exclude_keywords"] == ["stock", "price"]

            # Label follows the active tab.
            window.mode_tabs.setCurrentIndex(1)
            assert "Download" in window.run_btn.text()
            window.mode_tabs.setCurrentIndex(0)
            assert "Start" in window.run_btn.text()

            # Running disables the shared buttons and restores them after.
            window._set_all_running(True)
            assert not window.run_btn.isEnabled()
            assert not window.clear_btn.isEnabled()
            window._set_all_running(False)
            assert window.run_btn.isEnabled()
            assert window.clear_btn.isEnabled()
        finally:
            window.close()
    finally:
        for mod, original in zip(stubs, originals):
            mod.show_message = original


class _DummyWorker:
    def __init__(self):
        self.stop_requested = False

    def request_stop(self):
        self.stop_requested = True


def test_pair_table_shows_live_status():
    """The last pairing column turns into a status column while running."""
    _app()
    from PySide6.QtWidgets import QProgressBar

    from core.models import DownloadPair
    from core.progress import PAIR_DONE, PAIR_DOWNLOADING, PAIR_FAILED, PairStatus
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        tab = window.manual_tab
        tab._pairs = [
            DownloadPair(
                "pair_0001", "a.csv", "http://x/a.csv", "b.csv", "http://x/b.csv"
            ),
            DownloadPair(
                "pair_0002", "c.csv", "http://x/c.csv", "d.csv", "http://x/d.csv"
            ),
        ]
        tab._render_pairs()
        assert tab.pair_table.horizontalHeaderItem(4).text() == ""

        tab.set_running(True)
        assert tab.pair_status.active
        assert tab.pair_table.horizontalHeaderItem(4).text() == "Status"
        assert tab.pair_table.item(0, 4).text() == "Queued"

        tab.update_pair_status(PairStatus(0, PAIR_DOWNLOADING, 0.42, "1 MB / 2 MB"))
        bar = tab.pair_table.cellWidget(0, 4)
        assert bar is not None
        found = bar if isinstance(bar, QProgressBar) else bar.findChild(QProgressBar)
        assert found is not None and found.value() == 42

        tab.update_pair_status(PairStatus(0, PAIR_DONE, 1.0))
        assert "Done" in tab.pair_table.item(0, 4).text()

        tab.update_pair_status(PairStatus(1, PAIR_FAILED, 0.0, "", "HTTP 404"))
        failed_item = tab.pair_table.item(1, 4)
        assert "404" in failed_item.text()
        assert failed_item.toolTip() == "HTTP 404"

        tab.set_running(False)
        assert not tab.pair_status.active
        assert tab.pair_table.horizontalHeaderItem(4).text() == ""
        assert tab.pair_table.cellWidget(0, 4) is not None  # Remove button back
    finally:
        window._current_worker = None
        window.close()


def test_progress_update_drives_bar_and_status():
    _app()
    from core.progress import PHASE_DOWNLOAD, PHASE_VALIDATE, ProgressUpdate
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window._on_progress(
            ProgressUpdate(
                PHASE_DOWNLOAD,
                1,
                4,
                "feed_uk.csv vs feed_de.csv",
                0.5,
                "2.4 MB / 4.8 MB",
                base=0.0,
                span=0.6,
            )
        )
        assert window.progress_bar.value() == 30  # 60% * 50%
        text = window.status_label.text()
        assert "1/4" in text and "feed_uk.csv" in text and "2.4 MB" in text

        window._on_progress(
            ProgressUpdate(
                PHASE_VALIDATE,
                5,
                10,
                "feed_main.csv",
                0.5,
                base=0.6,
                span=0.4,
            )
        )
        assert window.progress_bar.value() == 80  # 60% + 40% * 50%
        assert "5/10" in window.status_label.text()
    finally:
        window._current_worker = None
        window.close()


def test_log_does_not_steal_status_while_running():
    """Progress owns the status line during a run; logs only append."""
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.status_label.setText("progress text")
        window._current_worker = _DummyWorker()
        window._on_api_log("log line while running")
        assert window.status_label.text() == "progress text"
        assert "log line while running" in window._log_lines

        window._current_worker = None
        window._on_api_log("log line while idle")
        assert window.status_label.text() == "log line while idle"
    finally:
        window._current_worker = None
        window.close()


def test_cancel_button_only_visible_during_run():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert not window.cancel_btn.isVisible()

        window._set_all_running(True)
        assert not window.cancel_btn.isHidden()
        assert window.cancel_btn.isEnabled()

        dummy = _DummyWorker()
        window._current_worker = dummy
        window.cancel_btn.click()
        assert dummy.stop_requested is True
        assert not window.cancel_btn.isEnabled()
        assert "Cancelling" in window.status_label.text()

        window._current_worker = None
        window._set_all_running(False)
        assert window.cancel_btn.isHidden()
    finally:
        window._current_worker = None
        window.close()


def test_no_empty_family_fonts():
    """Guard against fonts with an unset family.

    A QFont built with an empty family name resolves to the legacy
    "MS Sans Serif" bitmap face, which has no bold cut. Qt then logs a
    DirectWrite failure on every paint of that widget.
    """
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted(root.joinpath("ui").glob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if "QFont('')" in line or 'QFont("")' in line:
                offenders.append(f"{path.name}:{number}")
    assert not offenders, f"empty-family QFont at {offenders}"


def test_folder_labels_are_short_and_unambiguous():
    """File mode names the two sides after their folders."""
    from ui.workers import folder_labels

    assert folder_labels("D:/feeds/site_a", "D:/feeds/site_b") == ("site_a", "site_b")
    # Same basename on both sides: prepend the parent so they stay distinct.
    assert folder_labels("D:/old/site_a", "D:/new/site_a") == (
        "old/site_a",
        "new/site_a",
    )
    # Still identical: fall back to the full paths.
    first, second = folder_labels("D:/a/site", "E:/a/site")
    assert first != second


def test_rate_scope_switches_between_aggregate_and_single_pair():
    """The aggregate view cannot name its files; the picker must resolve it."""
    from ui.results_panel import ResultsPanel

    _app()
    results = _results()
    results["site1_label"] = "site_a"
    results["site2_label"] = "site_b"
    panel = ResultsPanel()
    panel.render(results)

    combo = panel.rate_scope_combo
    successful = [
        name
        for name, res in results["files"].items()
        if res.get("status") == "success" and res.get("column_stats")
    ]
    assert combo.count() == 1 + len(successful), combo.count()
    assert combo.itemText(0) == "All pairs"
    assert not panel.column_table.isColumnHidden(8)  # Pairs column visible
    assert "Aggregated across" in panel.rate_caption.text()

    # Pick the first individual pair: its own columns, no Pairs column.
    combo.setCurrentIndex(1)
    assert "compared in this pair" in panel.rate_caption.text()
    assert panel.column_table.isColumnHidden(8)
    pair_stats = results["files"][combo.itemText(1)]["column_stats"]
    assert panel.column_table.rowCount() == len(pair_stats)

    # Back to the aggregate restores the merged row count and the column.
    combo.setCurrentIndex(0)
    assert not panel.column_table.isColumnHidden(8)
    assert panel.column_table.rowCount() == len(results["overall_column_stats"])


def test_diff_preview_names_both_sides():
    """The two value columns must say which folder each side came from."""
    from ui.results_panel import ResultsPanel

    _app()
    results = _results()
    results["site1_label"] = "site_a"
    results["site2_label"] = "site_b"
    results["site1_path"] = "D:/feeds/site_a"
    results["site2_path"] = "D:/feeds/site_b"
    panel = ResultsPanel()
    panel.render(results)

    headers = [panel.diff_table.horizontalHeaderItem(i).text() for i in range(5)]
    assert headers[2] == "site_a", headers
    assert headers[3] == "site_b", headers
    caption = panel.diff_caption.text()
    assert "site_a" in caption and "site_b" in caption
    assert "D:/feeds/site_a" in panel.diff_caption.toolTip()
    assert "D:/feeds/site_b" in panel.diff_caption.toolTip()


def test_clear_resets_scope_picker():
    from ui.results_panel import ResultsPanel

    _app()
    panel = ResultsPanel()
    panel.render(_results())
    assert panel.rate_scope_combo.count() > 0
    panel.clear()
    assert panel.rate_scope_combo.count() == 0
    assert panel.rate_caption.text() == ""
    assert panel.diff_caption.text() == ""


def _stub_manual_modals(module):
    """Silence the modal prompts in the manual tab, returning the originals."""
    originals = (module.show_message, module.show_question)
    module.show_message = lambda *args, **kwargs: 0
    module.show_question = lambda *args, **kwargs: True
    return originals


def _restore_manual_modals(module, originals):
    module.show_message, module.show_question = originals


def _manual_tab_with_pairs(left_text, right_text):
    """Build a manual tab, fill both sides and generate the initial pairing."""
    import ui.manual_url_tab as manual_mod
    from ui.main_window import MainWindow

    originals = _stub_manual_modals(manual_mod)
    window = MainWindow()
    tab = window.manual_tab
    tab.url_left_edit.setPlainText(left_text)
    tab.url_right_edit.setPlainText(right_text)
    tab._on_generate_pairs()
    return window, tab, (manual_mod, originals)


def test_manual_generate_dedupes_urls_and_pairs_by_position():
    _app()
    window, tab, (module, originals) = _manual_tab_with_pairs(
        "https://x/a.csv\nhttps://x/b.csv\nhttps://x/a.csv",
        "https://y/x.csv\nhttps://y/y.csv",
    )
    try:
        assert tab._left_nodes == ["https://x/a.csv", "https://x/b.csv"]
        assert tab._right_nodes == ["https://y/x.csv", "https://y/y.csv"]
        assert tab._links == [(0, 0), (1, 1)]
        assert [p.pair_id for p in tab._pairs] == ["pair_0001", "pair_0002"]
        # Both views render the same model.
        assert tab.canvas.links() == tab._links
        assert tab.pair_table.rowCount() == 2
    finally:
        window.close()
        _restore_manual_modals(module, originals)


def test_manual_view_toggle_swaps_map_and_table():
    _app()
    window, tab, (module, originals) = _manual_tab_with_pairs(
        "https://x/a.csv",
        "https://y/x.csv",
    )
    try:
        # Map is the default editor; the table is hidden but still populated.
        assert not tab.canvas.isHidden()
        assert tab.pair_table.isHidden()
        assert tab.pair_table.rowCount() == 1

        tab.view_table_btn.click()
        assert tab.pair_table.isVisible() or not tab.pair_table.isHidden()
        assert tab.canvas.isHidden()

        tab.view_map_btn.click()
        assert tab.canvas.isHidden() is False
        assert tab.pair_table.isHidden()
    finally:
        window.close()
        _restore_manual_modals(module, originals)


def test_manual_editing_urls_marks_the_map_stale_without_dropping_links():
    _app()
    window, tab, (module, originals) = _manual_tab_with_pairs(
        "https://x/a.csv\nhttps://x/b.csv",
        "https://y/x.csv\nhttps://y/y.csv",
    )
    try:
        assert tab.stale_label.isHidden()

        tab.url_left_edit.setPlainText(
            "https://x/a.csv\nhttps://x/b.csv\nhttps://x/c.csv"
        )
        assert not tab.stale_label.isHidden()
        # The hand-built pairing survives until the user regenerates.
        assert len(tab._pairs) == 2

        tab._on_generate_pairs()
        assert tab.stale_label.isHidden()
        assert len(tab._pairs) == 2  # min(3 left, 2 right)
    finally:
        window.close()
        _restore_manual_modals(module, originals)


def test_manual_remove_row_updates_both_views():
    _app()
    window, tab, (module, originals) = _manual_tab_with_pairs(
        "https://x/a.csv\nhttps://x/b.csv",
        "https://y/x.csv\nhttps://y/y.csv",
    )
    try:
        tab._remove_pair(0)
        assert tab._links == [(1, 1)]
        assert tab.canvas.links() == [(1, 1)]
        assert tab.pair_table.rowCount() == 1
        assert tab._pairs[0].pair_id == "pair_0001"
    finally:
        window.close()
        _restore_manual_modals(module, originals)


def test_manual_canvas_edit_rebuilds_the_pair_list():
    """A drag in the map has to reach the table and the run payload."""
    _app()
    window, tab, (module, originals) = _manual_tab_with_pairs(
        "https://x/a.csv\nhttps://x/b.csv",
        "https://y/x.csv\nhttps://y/y.csv",
    )
    try:
        tab.canvas.links_changed.emit([(0, 1)])
        assert tab._links == [(0, 1)]
        assert len(tab._pairs) == 1
        assert tab._pairs[0].left_name == "a.csv"
        assert tab._pairs[0].right_name == "y.csv"
        assert tab.pair_table.rowCount() == 1
        assert (
            tab.build_run_payload(window.settings_widget.get_config())["pairs"][
                0
            ].right_url
            == "https://y/y.csv"
        )
    finally:
        window.close()
        _restore_manual_modals(module, originals)


def test_message_dialog_uses_badge_and_single_primary_action():
    """The flat dialog replaced QMessageBox, which looked nothing like the app."""
    _app()
    from PySide6.QtWidgets import QLabel, QPushButton

    from ui.dialogs import MessageDialog

    dialog = MessageDialog(None, "Validation Failed", "Something broke.", "error")
    try:
        badge = dialog.findChild(QLabel, "dlgBadgeError")
        assert badge is not None
        assert not badge.pixmap().isNull(), "severity glyph should render"
        assert dialog.findChild(QLabel, "dlgTitle").text() == "Validation Failed"
        assert dialog.findChild(QLabel, "dlgBody").text() == "Something broke."

        primary = dialog.findChildren(QPushButton, "dlgPrimaryBtn")
        assert [b.text() for b in primary] == ["OK"]
        assert not dialog.findChildren(QPushButton, "dlgSecondaryBtn")
        # Fixed width keeps short messages from stretching into a wide banner.
        assert dialog.width() == 440
    finally:
        dialog.deleteLater()


def test_question_dialog_defaults_to_the_safe_button():
    _app()
    from PySide6.QtWidgets import QPushButton

    from ui.dialogs import MessageDialog

    dialog = MessageDialog(
        None,
        "Replace Pairing",
        "Continue?",
        "question",
        [("No", False, True), ("Yes", True, False)],
    )
    try:
        secondary = dialog.findChildren(QPushButton, "dlgSecondaryBtn")
        primary = dialog.findChildren(QPushButton, "dlgPrimaryBtn")
        assert [b.text() for b in secondary] == ["No"]
        assert [b.text() for b in primary] == ["Yes"]
        # Enter must not confirm a destructive action.
        assert secondary[0].isDefault()
        assert not primary[0].isDefault()

        primary[0].click()
        assert dialog.choice == 1
    finally:
        dialog.deleteLater()


def test_app_icon_asset_loads():
    _app()
    from ui.theme import app_icon

    icon = app_icon()
    assert not icon.isNull()
    sizes = {size.width() for size in icon.availableSizes()}
    # The .ico ships every size, so the shell is not downscaling one bitmap.
    assert {16, 32, 256} <= sizes, sizes


def test_match_rate_keeps_two_decimals():
    """99.6% must not read as 100%: that would claim there are no differences."""
    from ui.format import format_percent

    assert format_percent(100) == "100.00%"
    assert format_percent(99.6) == "99.60%"
    assert format_percent(99.999) == "100.00%"  # rounded at the 2nd decimal
    assert format_percent(0.9) == "0.90%"
    assert format_percent(0) == "0.00%"
    assert format_percent(None) == "0.00%"
    assert format_percent(150) == "100.00%"  # clamped
    assert format_percent(-5) == "0.00%"


def test_export_differences_is_a_config_option():
    _app()
    from ui.config_widget import ComparisonConfigWidget

    widget = ComparisonConfigWidget(["stock"])
    assert widget.get_config()["export_diff"] is True  # on by default

    widget.export_diff_check.setChecked(False)
    assert widget.get_config()["export_diff"] is False

    widget.set_running(True)
    assert not widget.export_diff_check.isEnabled()  # locked mid-run
    widget.deleteLater()


def test_differences_tab_explains_a_disabled_export():
    """With the export off there are no files, and the tab has to say why."""
    _app()
    from ui.results_panel import ResultsPanel

    panel = ResultsPanel()
    panel.render(
        {
            "files": {
                "a.csv": {
                    "status": "success",
                    "pair_id": "p1",
                    "summary": {},
                    "column_stats": {"title": {}},
                    "diff_csv_path": "",
                },
            },
            "overall": {
                "total_pairs": 1,
                "success": 1,
                "skipped": 0,
                "total_skus": 1,
                "total_diffs": 0,
                "total_missing": 0,
                "avg_match_rate": 100,
            },
            "overall_column_stats": {},
            "debug_logs": [],
            "sku_detection_log": [],
            "source": "file",
            "diff_export_enabled": False,
        }
    )
    text = panel.diff_table.item(0, 0).text()
    assert "turned off" in text, text
    panel.deleteLater()


def test_differences_tab_says_nothing_to_show_when_export_was_on():
    _app()
    from ui.results_panel import ResultsPanel

    panel = ResultsPanel()
    panel.render(
        {
            "files": {
                "a.csv": {
                    "status": "success",
                    "pair_id": "p1",
                    "summary": {},
                    "column_stats": {},
                    "diff_csv_path": "",
                },
            },
            "overall": {
                "total_pairs": 1,
                "success": 1,
                "skipped": 0,
                "total_skus": 1,
                "total_diffs": 0,
                "total_missing": 0,
                "avg_match_rate": 100,
            },
            "overall_column_stats": {},
            "debug_logs": [],
            "sku_detection_log": [],
            "source": "file",
            "diff_export_enabled": True,
        }
    )
    text = panel.diff_table.item(0, 0).text()
    assert "No cell-level differences" in text, text
    panel.deleteLater()


def _horizontal(alignment) -> str:
    from PySide6.QtCore import Qt

    if alignment & Qt.AlignRight:
        return "right"
    if alignment & Qt.AlignHCenter:
        return "center"
    return "left"


def test_table_headers_align_with_their_cells():
    """Every column heading sits over the same edge as the values below it.

    Qt centres header text unless told otherwise, so a right-aligned number
    column ends up with its heading floating over the middle. Checked for the
    two data tables; the diff table is left-aligned throughout.
    """
    _app()
    from ui.results_panel import ResultsPanel

    panel = ResultsPanel()
    panel.render(_results())

    for name, table in (("rates", panel.column_table), ("files", panel.file_table)):
        assert table.rowCount() > 0, name
        for column in range(table.columnCount()):
            header = table.horizontalHeaderItem(column)
            cell = table.item(0, column)
            if header is None or cell is None:
                continue
            assert _horizontal(header.textAlignment()) == _horizontal(
                cell.textAlignment()
            ), (
                f"{name} column {column} ({header.text()}): header "
                f"{_horizontal(header.textAlignment())} vs cell "
                f"{_horizontal(cell.textAlignment())}"
            )
    panel.deleteLater()


def test_differences_tab_scopes_to_a_single_pair():
    """The preview used to mix every pair into one 100-row window."""
    _app()
    from ui.results_panel import ResultsPanel

    panel = ResultsPanel()
    results = _results()
    panel.render(results)

    combo = panel.diff_scope_combo
    pair_names = [name for name, _path in panel.get_diff_paths()]
    assert combo.count() == len(pair_names) + 1  # "All pairs" + each
    assert combo.itemText(0) == "All pairs"

    # Every individual scope shows only its own rows. Some pairs have no
    # differences at all, so the assertion is "whatever is shown belongs to
    # this pair", not "something is shown".
    scoped_rows = 0
    for index in range(1, combo.count()):
        combo.setCurrentIndex(index)
        pair = combo.itemText(index)
        shown = {
            panel.diff_table.item(row, 4).text()
            for row in range(panel.diff_table.rowCount())
            if panel.diff_table.item(row, 4) is not None
        }
        assert shown <= {pair}, (pair, shown)
        scoped_rows += panel.diff_table.rowCount()
    assert scoped_rows > 0, "no pair produced any preview rows"
    assert "this pair" in panel.diff_scope_note.text()

    # Back to the aggregate: rows from several pairs again.
    combo.setCurrentIndex(0)
    assert panel.diff_table.rowCount() > 0
    assert "pairs" in panel.diff_scope_note.text()
    panel.deleteLater()


def test_differences_tab_hides_the_picker_for_a_single_pair():
    _app()
    from ui.results_panel import ResultsPanel

    results = _results()
    one_name = next(iter(results["files"]))
    only_one = {
        "files": {one_name: results["files"][one_name]},
        "overall": results["overall"],
        "overall_column_stats": results["overall_column_stats"],
        "debug_logs": [],
        "sku_detection_log": [],
        "source": "file",
        "diff_export_enabled": True,
    }
    panel = ResultsPanel()
    panel.render(only_one)

    # One pair means one scope, and a disabled picker rather than a menu with
    # a single meaningless entry.
    assert panel.diff_scope_combo.count() == 1
    assert not panel.diff_scope_combo.isEnabled()
    panel.deleteLater()
