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

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtWidgets import QApplication, QScrollArea

from core.validator import FastBatchValidator

import make_test_data as gen


DATA = Path(__file__).resolve().parent / 'data'
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
    root = Path(tempfile.mkdtemp(prefix='feed_ui_test_'))
    _TEMP_ROOTS.append(root)
    atexit.register(shutil.rmtree, root, True)
    return root


def _results():
    _app()
    if not (DATA / gen.SITE_A).exists():
        gen.build(DATA)
    validator = FastBatchValidator(
        str(DATA / gen.SITE_A), str(DATA / gen.SITE_B), None, [],
        exclude_keywords=[], temp_diff_dir=_temp_root(),
    )
    results = validator.validate_all_parallel()
    results['cleanup'] = validator.cleanup
    results['site1_label'] = ''
    results['site2_label'] = ''
    results['source'] = 'file'
    results['api_log_lines'] = ['log line 1', 'log line 2']
    return results


def test_main_window_builds():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert window.mode_tabs.count() == 3
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
    widget = ComparisonConfigWidget(exclude_keywords=['stock', 'price'])
    config = widget.get_config()
    assert config['exclude_keywords'] == ['stock', 'price'], config
    assert config['workers'] == 4
    assert config['auto_detect'] is True

    # The field is user-editable and drives the run.
    widget.exclude_edit.setText('promo, sale')
    assert widget.get_config()['exclude_keywords'] == ['promo', 'sale']

    widget.exclude_edit.setText('')
    assert widget.get_config()['exclude_keywords'] == []


def test_results_panel_renders_all_tabs():
    from ui.results_panel import ResultsPanel

    _app()
    results = _results()
    panel = ResultsPanel()
    panel.render(results)

    assert panel.column_table.rowCount() > 0
    assert panel.file_table.rowCount() == len(results['files'])
    assert panel.diff_table.rowCount() > 0
    assert 'VALIDATION SUMMARY' in panel.summary_text.toPlainText()
    assert 'DEBUG LOG' in panel.log_text.toPlainText()
    assert panel.get_diff_paths(), 'diff CSVs should be discoverable'


def test_results_panel_handles_empty_results():
    from ui.results_panel import ResultsPanel

    _app()
    panel = ResultsPanel()
    panel.render({
        'files': {}, 'overall': {}, 'overall_column_stats': {},
        'debug_logs': [], 'sku_detection_log': [], 'source': 'file',
    })
    assert panel.column_table.rowCount() == 1      # single message row
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
    panel.show_render_error('Traceback: boom')
    assert 'boom' in panel.summary_text.toPlainText()


def test_export_writes_expected_archive_members():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        results = _results()
        window._current_results = results
        window.results_panel.render(results)

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'report.zip'
            window._export_to_zip(str(target), results)

            assert target.exists()
            with zipfile.ZipFile(target) as zf:
                names = set(zf.namelist())
                assert 'Summary.csv' in names, names
                assert 'Files.csv' in names, names
                assert 'Column_Match_Rates.csv' in names, names
                assert 'Debug_Log.csv' in names, names
                assert any(n.startswith('Differences_') for n in names), names

                diff_name = next(n for n in names if n.startswith('Differences_'))
                header = zf.read(diff_name).decode('utf-8-sig').splitlines()[0]
                assert header == 'sku,column,Value_F1,Value_F2', header

                summary = zf.read('Summary.csv').decode('utf-8-sig')
                assert 'total_pairs' in summary
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
        assert 'Cancelled' in window.status_label.text()
        assert window._current_worker is None
    finally:
        window.close()


def test_file_filter_only_visible_in_file_mode():
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        assert not window.file_tab.config_widget.filter_combo.isHidden()
        assert window.api_tab.config_widget.filter_combo.isHidden()
        assert window.manual_tab.config_widget.filter_combo.isHidden()
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
            assert window.file_tab.build_run_payload() is None

            window.file_tab.f1_edit.setText('tests/data/site_a')
            window.file_tab.f2_edit.setText('tests/data/site_b')
            payload = window.file_tab.build_run_payload()
            assert payload['mode'] == 'file'
            assert payload['config']['exclude_keywords'] == ['stock', 'price']

            # Label follows the active tab.
            window.mode_tabs.setCurrentIndex(1)
            assert 'Download' in window.run_btn.text()
            window.mode_tabs.setCurrentIndex(0)
            assert 'Start' in window.run_btn.text()

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
    from core.progress import (
        PAIR_DONE, PAIR_DOWNLOADING, PAIR_FAILED, PairStatus,
    )
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        tab = window.manual_tab
        tab._pairs = [
            DownloadPair('pair_0001', 'a.csv', 'http://x/a.csv',
                         'b.csv', 'http://x/b.csv'),
            DownloadPair('pair_0002', 'c.csv', 'http://x/c.csv',
                         'd.csv', 'http://x/d.csv'),
        ]
        tab._render_pairs()
        assert tab.pair_table.horizontalHeaderItem(4).text() == ''

        tab.set_running(True)
        assert tab.pair_status.active
        assert tab.pair_table.horizontalHeaderItem(4).text() == 'Status'
        assert tab.pair_table.item(0, 4).text() == 'Queued'

        tab.update_pair_status(PairStatus(0, PAIR_DOWNLOADING, 0.42, '1 MB / 2 MB'))
        bar = tab.pair_table.cellWidget(0, 4)
        assert bar is not None
        found = bar if isinstance(bar, QProgressBar) else bar.findChild(QProgressBar)
        assert found is not None and found.value() == 42

        tab.update_pair_status(PairStatus(0, PAIR_DONE, 1.0))
        assert 'Done' in tab.pair_table.item(0, 4).text()

        tab.update_pair_status(PairStatus(1, PAIR_FAILED, 0.0, '', 'HTTP 404'))
        failed_item = tab.pair_table.item(1, 4)
        assert '404' in failed_item.text()
        assert failed_item.toolTip() == 'HTTP 404'

        tab.set_running(False)
        assert not tab.pair_status.active
        assert tab.pair_table.horizontalHeaderItem(4).text() == ''
        assert tab.pair_table.cellWidget(0, 4) is not None   # Remove button back
    finally:
        window._current_worker = None
        window.close()


def test_progress_update_drives_bar_and_status():
    _app()
    from core.progress import PHASE_DOWNLOAD, PHASE_VALIDATE, ProgressUpdate
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window._on_progress(ProgressUpdate(
            PHASE_DOWNLOAD, 1, 4, 'feed_uk.csv vs feed_de.csv', 0.5, '2.4 MB / 4.8 MB',
            base=0.0, span=0.6,
        ))
        assert window.progress_bar.value() == 30          # 60% * 50%
        text = window.status_label.text()
        assert '1/4' in text and 'feed_uk.csv' in text and '2.4 MB' in text

        window._on_progress(ProgressUpdate(
            PHASE_VALIDATE, 5, 10, 'feed_main.csv', 0.5, base=0.6, span=0.4,
        ))
        assert window.progress_bar.value() == 80          # 60% + 40% * 50%
        assert '5/10' in window.status_label.text()
    finally:
        window._current_worker = None
        window.close()


def test_log_does_not_steal_status_while_running():
    """Progress owns the status line during a run; logs only append."""
    _app()
    from ui.main_window import MainWindow

    window = MainWindow()
    try:
        window.status_label.setText('progress text')
        window._current_worker = _DummyWorker()
        window._on_api_log('log line while running')
        assert window.status_label.text() == 'progress text'
        assert 'log line while running' in window._log_lines

        window._current_worker = None
        window._on_api_log('log line while idle')
        assert window.status_label.text() == 'log line while idle'
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
        assert 'Cancelling' in window.status_label.text()

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
    for path in sorted(root.joinpath('ui').glob('*.py')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith('#'):
                continue
            if "QFont('')" in line or 'QFont("")' in line:
                offenders.append(f'{path.name}:{number}')
    assert not offenders, f'empty-family QFont at {offenders}'
