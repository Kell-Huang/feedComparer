"""Large-batch and hard-edge-case tests.

Fixtures are generated into a temp directory on first use (see
``scale_data.py``) and removed at exit; nothing large is committed.

Timing budgets are deliberately loose — roughly 15x the measured time on the
development machine. They are regression guards against an accidental
quadratic or a per-row Python loop, not benchmarks, so they should not be
flaky on slower hardware. The measured value is printed on failure.
"""
import ctypes
import ctypes.wintypes as wt
import time
from pathlib import Path

import scale_data
from core.validator import FastBatchValidator


BIG_BUDGET_S = 15.0
WIDE_BUDGET_S = 15.0
MANY_BUDGET_S = 25.0
PEAK_MEMORY_BUDGET_MB = 1200.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [
        ('cb', wt.DWORD),
        ('PageFaultCount', wt.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
    ]


_GET_MEMORY = ctypes.windll.psapi.GetProcessMemoryInfo
_GET_MEMORY.argtypes = [wt.HANDLE, ctypes.POINTER(_ProcessMemoryCounters), wt.DWORD]
_GET_MEMORY.restype = wt.BOOL


def peak_memory_mb() -> float:
    """Peak working set of this process, in MiB."""
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    ok = _GET_MEMORY(
        ctypes.windll.kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        counters.cb,
    )
    if not ok:
        return 0.0
    return counters.PeakWorkingSetSize / (1024 * 1024)


def _validate(folder_name, **kwargs):
    a_dir, b_dir = scale_data.folder(folder_name)
    kwargs.setdefault('exclude_keywords', [])
    kwargs.setdefault('debug', False)
    started = time.perf_counter()
    validator = FastBatchValidator(str(a_dir), str(b_dir), None, [], **kwargs)
    try:
        results = validator.validate_all_parallel()
    finally:
        validator.cleanup()
    return results, time.perf_counter() - started


def _assert_counters_consistent(results):
    """Every column's match + mismatch must equal its compared total."""
    for name, result in results['files'].items():
        if result['status'] != 'success':
            continue
        for column, stats in result['column_stats'].items():
            assert stats['exact_matches'] + stats['mismatches'] == stats['total_compared'], (
                name, column, stats)
            assert stats['both_nan'] <= stats['exact_matches'], (name, column, stats)


# ---------------------------------------------------------------------------
# Scale
# ---------------------------------------------------------------------------

def test_big_pair_exact_counts_and_budget():
    """150k rows x 20 columns: exact diff counts plus a runtime guard."""
    expected = scale_data.big_expected()
    results, elapsed = _validate('big')

    overall = results['overall']
    assert overall['total_pairs'] == 1
    assert overall['success'] == 1, overall

    result = results['files']['feed.csv']
    assert result['status'] == 'success', result
    assert result['sku_col1'] == 'sku'

    summary = result['summary']
    for key in ('rows_file1', 'rows_file2', 'common_skus',
                'only_in_file1', 'only_in_file2', 'columns_compared'):
        assert summary[key] == expected[key], (key, summary[key], expected[key])

    stats = result['column_stats']
    assert stats['price']['mismatches'] == expected['price_diffs'], stats['price']
    assert stats['title']['mismatches'] == expected['title_diffs'], stats['title']
    assert summary['total_differences'] == expected['total_diffs'], summary

    # The sparse "note" column is empty on 9 of 10 rows on both sides; those
    # must count as matches, not mismatches.
    assert stats['note']['mismatches'] == 0, stats['note']
    assert stats['note']['both_nan'] == expected['common_skus'] - expected['common_skus'] // 10

    _assert_counters_consistent(results)

    assert elapsed < BIG_BUDGET_S, (
        f'{elapsed:.1f}s exceeds the {BIG_BUDGET_S}s budget for 150k x 20')


def test_peak_memory_stays_bounded():
    """Guard against a run whose memory grows with the whole dataset at once."""
    peak = peak_memory_mb()
    assert peak < PEAK_MEMORY_BUDGET_MB, (
        f'peak working set {peak:.0f} MB exceeds {PEAK_MEMORY_BUDGET_MB:.0f} MB')


def test_wide_feed():
    """40k rows x 119 columns: column explosion must not degrade the diff."""
    results, elapsed = _validate('wide')
    result = results['files']['wide.csv']
    assert result['status'] == 'success', result
    summary = result['summary']
    assert summary['columns_compared'] == scale_data.WIDE_ATTRS, summary
    # attr_7 differs every 2000 rows.
    expected = scale_data.WIDE_ROWS // 2000
    assert summary['total_differences'] == expected, (summary, expected)
    assert result['column_stats']['attr_7']['mismatches'] == expected
    _assert_counters_consistent(results)
    assert elapsed < WIDE_BUDGET_S, f'{elapsed:.1f}s exceeds the {WIDE_BUDGET_S}s budget'


def test_many_pairs_aggregate():
    """40 pairs: overall totals must equal the sum over individual files."""
    results, elapsed = _validate('many', max_workers=4)
    overall = results['overall']
    assert overall['total_pairs'] == scale_data.MANY_PAIRS, overall
    assert overall['success'] == scale_data.MANY_PAIRS, overall
    assert overall['failed'] == 0 and overall['skipped'] == 0, overall

    files = results['files']
    assert len(files) == scale_data.MANY_PAIRS
    summed_diffs = sum(r['summary']['total_differences'] for r in files.values())
    summed_skus = sum(r['summary']['common_skus'] for r in files.values())
    assert overall['total_diffs'] == summed_diffs, (overall['total_diffs'], summed_diffs)
    assert overall['total_skus'] == summed_skus, (overall['total_skus'], summed_skus)

    # Every even-numbered pair has 4 price differences at 2000 rows / 500.
    expected_diffs = (scale_data.MANY_PAIRS // 2) * (scale_data.MANY_ROWS // 500)
    assert overall['total_diffs'] == expected_diffs, (overall['total_diffs'], expected_diffs)
    _assert_counters_consistent(results)
    assert elapsed < MANY_BUDGET_S, f'{elapsed:.1f}s exceeds the {MANY_BUDGET_S}s budget'


def test_large_xml():
    """10k <item> elements with a namespace."""
    results, _elapsed = _validate('xml')
    result = results['files']['feed.xml']
    assert result['status'] == 'success', result
    summary = result['summary']
    assert summary['rows_file1'] == scale_data.XML_ITEMS, summary
    assert summary['common_skus'] == scale_data.XML_ITEMS, summary
    assert summary['total_differences'] == scale_data.XML_ITEMS // 1000, summary
    assert result['sku_col1'] == 'id', result


def test_large_excel():
    """15k rows through the Excel reader."""
    results, _elapsed = _validate('xlsx')
    result = results['files']['feed.xlsx']
    assert result['status'] == 'success', result
    summary = result['summary']
    assert summary['rows_file1'] == scale_data.XLSX_ROWS, summary
    assert summary['total_differences'] == scale_data.XLSX_ROWS // 1500, summary


def test_large_zip():
    """80k rows inside a ZIP member."""
    results, _elapsed = _validate('zip')
    result = results['files']['feed.zip']
    assert result['status'] == 'success', result
    summary = result['summary']
    assert summary['rows_file1'] == scale_data.ZIP_ROWS, summary
    assert summary['total_differences'] == scale_data.ZIP_ROWS // 2000, summary


def test_cancellation_during_large_run():
    """Cancelling a 150k-row run must return promptly and collect nothing."""
    import threading

    a_dir, b_dir = scale_data.folder('big')
    event = threading.Event()
    event.set()
    started = time.perf_counter()
    validator = FastBatchValidator(
        str(a_dir), str(b_dir), None, [], exclude_keywords=[], debug=False,
    )
    try:
        results = validator.validate_all_parallel(cancel_event=event)
    finally:
        validator.cleanup()
    elapsed = time.perf_counter() - started

    assert results['overall']['success'] == 0, results['overall']
    assert results['overall']['total_pairs'] == 1
    assert elapsed < 10.0, f'cancellation took {elapsed:.1f}s'


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def _edge_results():
    results, _elapsed = _validate('edge')
    return results


def test_edge_all_files_processed():
    results = _edge_results()
    assert results['overall']['total_pairs'] == 6, results['overall']
    assert results['overall']['failed'] == 0, results['errors']
    assert results['overall']['skipped'] == 0, results['skipped']


def test_edge_header_only_file():
    results = _edge_results()
    result = results['files']['empty.csv']
    assert result['status'] == 'success', result
    assert result['summary']['common_skus'] == 0
    assert result['summary']['total_differences'] == 0


def test_edge_bom_and_crlf():
    results = _edge_results()
    result = results['files']['bom.csv']
    assert result['status'] == 'success', result
    assert result['summary']['common_skus'] == 2
    assert result['summary']['total_differences'] == 0, result['summary']


def test_edge_reordered_columns_match_by_name():
    results = _edge_results()
    result = results['files']['reordered.csv']
    assert result['status'] == 'success', result
    # Column order differs between the files; alignment is by name.
    assert result['summary']['columns_compared'] == 2, result['summary']
    assert result['summary']['total_differences'] == 0, result['summary']


def test_edge_extra_column_on_one_side_is_ignored():
    results = _edge_results()
    result = results['files']['extra.csv']
    assert result['status'] == 'success', result
    # only_a exists on one side only, so it cannot be compared.
    assert result['summary']['columns_compared'] == 1, result['summary']
    assert result['summary']['total_differences'] == 1, result['summary']


def test_edge_all_blank_skus_do_not_align():
    """Blank SKUs must not be joined to each other as if they were one row."""
    results = _edge_results()
    result = results['files']['nullsku.csv']
    assert result['status'] == 'success', result
    assert result['summary']['common_skus'] == 0, result['summary']
    assert result['summary']['only_in_file2'] == 2, result['summary']
    assert result['summary']['total_differences'] == 0, result['summary']


def test_edge_unicode_values():
    """Unicode survives; genuinely different byte sequences stay different."""
    results = _edge_results()
    result = results['files']['unicode.csv']
    assert result['status'] == 'success', result
    summary = result['summary']
    assert summary['common_skus'] == 6, summary
    stats = result['column_stats']
    # One price differs; two titles differ (combining vs precomposed accent,
    # and a zero-width space that NFKC does not remove).
    assert stats['price']['mismatches'] == 1, stats['price']
    assert stats['title']['mismatches'] == 2, stats['title']
    assert summary['total_differences'] == 3, summary


def test_edge_diff_files_are_written():
    a_dir, b_dir = scale_data.folder('edge')
    validator = FastBatchValidator(
        str(a_dir), str(b_dir), None, [], exclude_keywords=[], debug=False,
    )
    try:
        results = validator.validate_all_parallel()
        for name, result in results['files'].items():
            if result['status'] != 'success':
                continue
            path = Path(result['diff_csv_path'])
            assert path.exists(), (name, path)
            header = path.read_text(encoding='utf-8-sig').splitlines()[0]
            assert header == 'sku,column,value_file1,value_file2', (name, header)
    finally:
        validator.cleanup()
