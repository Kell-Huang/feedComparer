"""Column-level diff statistics and diff CSV streaming.

Baseline comparison semantics:

  - Empty values (None / NaN / "") on both sides are treated as matching.
  - Numeric-like values are compared via float() fallback, so ``100``
    equals ``"100"`` and ``100.5`` equals ``"100.5"``.
    Note: float() loses precision beyond ~15 significant digits, so very
    large integer IDs may compare equal when they differ in low digits.
    This is a pre-existing behavior and is preserved intentionally.
  - Lists are compared by exact sequence, not as sets.

Normalization enhancements (only add matches; never introduce mismatches):

  - Bool: ``True``/``False`` match ``"true"``/``"false"`` (case-insensitive).
    Plain ``1``/``"1"`` are NOT treated as bool.
  - Date/datetime: native objects and ISO-like strings are unified via
    _to_datetime. Native date -> midnight datetime. Native datetime objects
    compare at full precision (no day-level collapsing).
      * Both sides are strings: only separator formats are accepted
        (``%Y-%m-%d`` / ``%Y/%m/%d``). Compact ``%Y%m%d`` is rejected here
        to avoid misclassifying numeric product codes as dates.
      * At least one side is native date/datetime: compact ``%Y%m%d`` is
        also accepted, since the other side is unambiguously date-like.
      * String fractional seconds beyond microseconds are truncated; Polars
        Datetime('ns') -> String casts are handled.
  - Thousands separators: strict ``\\d{1,3}(,\\d{3})+`` format only.
  - Full-width digits: ``"１００"`` matches ``100`` via NFKC.

Known edge case: two non-date strings that both parse to the same day via
separator formats (e.g. ``"2024-01-01"`` vs ``"2024/01/01"``) are treated
as equal. Exclude such columns from comparison if this is undesired.

Performance
-----------
Comparison runs in two tiers. Tier one is fully vectorized: empty detection
and exact string equality are evaluated by Polars over the whole column. Only
the cells that survive tier one (i.e. the ones that look different) are handed
to the Python equivalence rules. On real feeds the vast majority of cells are
identical, so this removes the per-cell interpreter overhead that previously
dominated runtime.
"""
import csv
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from typing import Dict, List

import polars as pl


class ColumnMatchStats:
    """Column-level diff statistics."""

    _THOUSANDS_RE = re.compile(r'^[+-]?\d{1,3}(,\d{3})+(\.\d+)?$')
    _SEP_DATE_FORMATS = ('%Y-%m-%d', '%Y/%m/%d')
    _COMPACT_DATE_FORMAT = '%Y%m%d'
    _ISO_DATETIME_FORMATS = (
        '%Y-%m-%d %H:%M:%S',
        '%Y-%m-%d %H:%M:%S.%f',
        '%Y-%m-%dT%H:%M:%S',
        '%Y-%m-%dT%H:%M:%S.%f',
    )
    _NANOSECOND_RE = re.compile(r'(\.\d{6})\d+')
    _BUFFER_FLUSH_SIZE = 5000

    @staticmethod
    def _is_empty(val) -> bool:
        """Check if a value is empty (None, NaN, or empty string)."""
        if val is None:
            return True
        if isinstance(val, float) and val != val:  # NaN without numpy
            return True
        if val == '':
            return True
        return False

    @staticmethod
    def _to_bool(val):
        """Return bool only for unambiguous bool representations, else None."""
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            s = val.strip().lower()
            if s == 'true':
                return True
            if s == 'false':
                return False
        return None

    @staticmethod
    def _normalize_nanos(s: str) -> str:
        """Truncate fractional seconds beyond 6 digits (microseconds)."""
        return ColumnMatchStats._NANOSECOND_RE.sub(r'\1', s)

    @staticmethod
    def _to_datetime(val, allow_compact: bool):
        """Parse to datetime for comparison. Returns None if not parseable.

        - Native datetime: returned as-is (full precision preserved).
        - Native date: promoted to midnight of that day.
        - Strings: separator date formats always accepted; compact YYYYMMDD
          accepted only when ``allow_compact`` is True (i.e. at least one
          side of the comparison is a native date/datetime).
        """
        if isinstance(val, datetime):
            return val
        if isinstance(val, date):
            return datetime(val.year, val.month, val.day)
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return None
            for fmt in ColumnMatchStats._SEP_DATE_FORMATS:
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
            if allow_compact:
                try:
                    return datetime.strptime(s, ColumnMatchStats._COMPACT_DATE_FORMAT)
                except ValueError:
                    pass
            s_trunc = ColumnMatchStats._normalize_nanos(s)
            for fmt in ColumnMatchStats._ISO_DATETIME_FORMATS:
                try:
                    return datetime.strptime(s_trunc, fmt)
                except ValueError:
                    continue
        return None

    @staticmethod
    def _values_equal(v1, v2) -> bool:
        # Fast path: direct Python equality.
        if v1 == v2:
            return True

        # Numeric fallback. Failures here do NOT short-circuit, so later
        # normalization paths (bool/date) can still run.
        try:
            return float(v1) == float(v2)
        except (ValueError, TypeError):
            pass

        # Enhancement: full-width digits via NFKC (only when non-ASCII).
        try:
            s1 = str(v1)
            s2 = str(v2)
            if not s1.isascii() or not s2.isascii():
                n1 = float(unicodedata.normalize('NFKC', s1).strip())
                n2 = float(unicodedata.normalize('NFKC', s2).strip())
                return n1 == n2
        except (ValueError, TypeError):
            pass

        # Enhancement: strict thousands separators.
        try:
            s1_raw = str(v1).strip()
            s2_raw = str(v2).strip()
            s1_clean = (s1_raw.replace(',', '')
                        if ColumnMatchStats._THOUSANDS_RE.match(s1_raw) else s1_raw)
            s2_clean = (s2_raw.replace(',', '')
                        if ColumnMatchStats._THOUSANDS_RE.match(s2_raw) else s2_raw)
            if s1_clean != s1_raw or s2_clean != s2_raw:
                return float(s1_clean) == float(s2_clean)
        except (ValueError, TypeError):
            pass

        # Enhancement: bool normalization.
        b1 = ColumnMatchStats._to_bool(v1)
        b2 = ColumnMatchStats._to_bool(v2)
        if b1 is not None and b2 is not None:
            return b1 == b2

        # Enhancement: date/datetime normalization.
        # Two native datetimes already failed exact equality, so no further
        # comparison path should reinterpret them.
        if isinstance(v1, datetime) and isinstance(v2, datetime):
            return False
        # Compact YYYYMMDD is only accepted when at least one side is native.
        both_strings = isinstance(v1, str) and isinstance(v2, str)
        allow_compact = not both_strings
        dt1 = ColumnMatchStats._to_datetime(v1, allow_compact=allow_compact)
        dt2 = ColumnMatchStats._to_datetime(v2, allow_compact=allow_compact)
        if dt1 is not None and dt2 is not None:
            return dt1 == dt2

        return False

    # ------------------------------------------------------------------
    # Vectorized helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _empty_mask(series: pl.Series) -> pl.Series:
        """Boolean mask of values that count as empty."""
        dtype = series.dtype
        if dtype == pl.String:
            return series.is_null() | (series == '')
        if dtype.is_float():
            return series.is_null() | series.is_nan()
        return series.is_null()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @staticmethod
    def calculate(df1_common: pl.DataFrame, df2_common: pl.DataFrame, sku_col1, sku_col2,
                  compare_cols: List[str], diff_csv_path: str) -> Dict:
        """Compare columns and stream differences to a CSV file.

        Precondition: df1_common and df2_common are row-aligned on _sku_norm
        (guaranteed by FastBatchValidator._align_dataframes).

        Differences are buffered and flushed in batches to reduce I/O overhead.
        """
        stats = {}
        skus = df1_common['_sku_norm'].to_list()
        values_equal = ColumnMatchStats._values_equal

        with open(diff_csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['sku', 'column', 'value_file1', 'value_file2'])

            buffer = []

            def flush():
                if buffer:
                    writer.writerows(buffer)
                    buffer.clear()

            for col in compare_cols:
                if col not in df1_common.columns or col not in df2_common.columns:
                    continue

                s1 = df1_common[col]
                s2 = df2_common[col]

                e1 = ColumnMatchStats._empty_mask(s1)
                e2 = ColumnMatchStats._empty_mask(s2)

                both_empty = e1 & e2
                only1 = e1 & ~e2
                only2 = ~e1 & e2

                t1 = s1.cast(pl.String, strict=False).fill_null('')
                t2 = s2.cast(pl.String, strict=False).fill_null('')

                # Tier one: cells that are non-empty and textually identical
                # are exact matches without any Python involvement.
                same_text = t1 == t2
                exact_fast = (~e1) & (~e2) & same_text
                # Tier two candidates: non-empty on both sides, different text.
                need_check = (~e1) & (~e2) & (~same_text)

                both_nan = int(both_empty.sum())
                only1_count = int(only1.sum())
                only2_count = int(only2.sum())

                need_idx = need_check.arg_true().to_list()
                failing = set()
                if need_idx:
                    sub1 = s1.gather(need_idx).to_list()
                    sub2 = s2.gather(need_idx).to_list()
                    for pos, idx in enumerate(need_idx):
                        if not values_equal(sub1[pos], sub2[pos]):
                            failing.add(idx)

                # Tier-two rows that passed the equivalence rules are matches
                # too. Omitting them made the counters fail to add up to
                # total_compared (e.g. "11.00" vs "11" counted as neither).
                equivalent = len(need_idx) - len(failing)
                exact_matches = both_nan + int(exact_fast.sum()) + equivalent
                mismatches = only1_count + only2_count + len(failing)
                total_compared = len(skus)

                # Emit mismatch rows in DataFrame order for a stable diff file.
                only1_idx = only1.arg_true().to_list()
                only2_idx = only2.arg_true().to_list()
                if only1_idx or only2_idx or failing:
                    mismatch_idx = sorted(set(only1_idx) | set(only2_idx) | failing)
                    v1_all = s1.gather(mismatch_idx).to_list()
                    v2_all = s2.gather(mismatch_idx).to_list()
                    e1_sel = e1.gather(mismatch_idx).to_list()
                    e2_sel = e2.gather(mismatch_idx).to_list()
                    for pos, idx in enumerate(mismatch_idx):
                        if e1_sel[pos] and not e2_sel[pos]:
                            row = [str(skus[idx]), col, 'EMPTY', str(v2_all[pos])]
                        elif e2_sel[pos] and not e1_sel[pos]:
                            row = [str(skus[idx]), col, str(v1_all[pos]), 'EMPTY']
                        else:
                            row = [str(skus[idx]), col,
                                   str(v1_all[pos]), str(v2_all[pos])]
                        buffer.append(row)
                        if len(buffer) >= ColumnMatchStats._BUFFER_FLUSH_SIZE:
                            flush()

                match_rate = (exact_matches / total_compared * 100) if total_compared > 0 else 0
                if match_rate == 100:
                    status = 'PERFECT'
                elif match_rate >= 95:
                    status = 'GOOD'
                elif match_rate >= 80:
                    status = 'FAIR'
                elif match_rate >= 50:
                    status = 'POOR'
                else:
                    status = 'BAD'

                stats[col] = {
                    'total_compared': total_compared,
                    'exact_matches': exact_matches,
                    'mismatches': mismatches,
                    'both_nan': both_nan,
                    'only_file1_nan': only1_count,
                    'only_file2_nan': only2_count,
                    'match_rate': round(match_rate, 2),
                    'status': status,
                }

            flush()
        return stats

    @staticmethod
    def calculate_overall(all_column_stats):
        overall = defaultdict(lambda: {
            'total_compared': 0, 'exact_matches': 0, 'mismatches': 0,
            'both_nan': 0, 'only_file1_nan': 0, 'only_file2_nan': 0,
            'file_count': 0,
        })
        for file_stats in all_column_stats:
            for col, stats in file_stats.items():
                overall[col]['total_compared'] += stats['total_compared']
                overall[col]['exact_matches'] += stats['exact_matches']
                overall[col]['mismatches'] += stats['mismatches']
                overall[col]['both_nan'] += stats.get('both_nan', 0)
                overall[col]['only_file1_nan'] += stats.get('only_file1_nan', 0)
                overall[col]['only_file2_nan'] += stats.get('only_file2_nan', 0)
                overall[col]['file_count'] += 1
        for col in overall:
            total = overall[col]['total_compared']
            matches = overall[col]['exact_matches']
            overall[col]['match_rate'] = round(matches / total * 100, 2) if total > 0 else 0
            rate = overall[col]['match_rate']
            if rate == 100:
                overall[col]['status'] = 'PERFECT'
            elif rate >= 95:
                overall[col]['status'] = 'GOOD'
            elif rate >= 80:
                overall[col]['status'] = 'FAIR'
            elif rate >= 50:
                overall[col]['status'] = 'POOR'
            else:
                overall[col]['status'] = 'BAD'
        return dict(overall)
