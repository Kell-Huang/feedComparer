"""Batch validator: align DataFrames by normalized SKU and diff common columns.

File reading is delegated to ``FileReader``; diff logic lives in
``ColumnMatchStats``. This class only orchestrates the pipeline:
discover pairs -> read -> normalize SKU -> align -> diff -> aggregate.

Cancellation: ``validate_all_parallel`` accepts an optional ``cancel_event``
(``threading.Event``). When set, the coordinator loop stops collecting new
results from in-flight futures. Currently running pair validations complete
for consistency, but no new pairs are started.

Memory: DataFrames are released as soon as a pair is scored. There is no
explicit ``gc.collect()`` in the hot loop — reference counting already frees
the frames, and forcing a full generational collection per file serialises
the worker pool and dominated runtime on large batches.
"""
import hashlib
import re
import shutil
import tempfile
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import polars as pl

from .logger import DebugLogger
from .normalizer import SKUNormalizer
from .progress import PHASE_VALIDATE, ProgressUpdate
from .reader import SUPPORTED_EXTENSIONS, FileReader
from .sku_detector import SKUColumnDetector
from .stats import ColumnMatchStats


class FastBatchValidator:
    def __init__(self, folder1, folder2, sku_column, compare_columns,
                 file_pattern=None, max_workers=4,
                 normalize_sku=True, case_sensitive=True, debug=True,
                 auto_detect_sku=True, priority_list=None,
                 exclude_keywords=None, temp_diff_dir=None):
        self.folder1 = Path(folder1)
        self.folder2 = Path(folder2)
        self.user_sku_column = sku_column
        self.compare_columns = compare_columns
        self.file_pattern = file_pattern
        self.max_workers = max(1, int(max_workers))
        self.normalize_sku = normalize_sku
        self.case_sensitive = case_sensitive
        self.debug = debug
        self.auto_detect_sku = auto_detect_sku
        self.exclude_keywords = exclude_keywords or []
        # Deferred: only create / adopt a temp dir after folders are validated,
        # otherwise a failed constructor leaks an empty mkdtemp().
        self.temp_diff_dir = temp_diff_dir
        self.lock = threading.Lock()
        self.logger = DebugLogger()
        self.logger.enabled = debug
        self.normalizer = SKUNormalizer()
        self.file_reader = FileReader(self.logger)
        self.sku_detector = SKUColumnDetector(
            user_specified=sku_column if sku_column else None,
            priority_list=priority_list,
        )
        self.logger.info("=" * 70, filename="SYSTEM")
        self.logger.info("VALIDATOR INITIALIZED", filename="SYSTEM")
        self.logger.info(f"Baseline folder : {self.folder1}", filename="SYSTEM")
        self.logger.info(f"Comparison folder: {self.folder2}", filename="SYSTEM")
        self.logger.info(f"SKU column (user): '{sku_column if sku_column else 'AUTO-DETECT'}'", filename="SYSTEM")
        if priority_list:
            self.logger.info(f"Priority list: {priority_list}", filename="SYSTEM")
        self.logger.info(f"Auto-detect SKU : {'ON' if auto_detect_sku else 'OFF'}", filename="SYSTEM")
        self.logger.info(f"Compare columns : {compare_columns if compare_columns else 'ALL common'}", filename="SYSTEM")
        if self.exclude_keywords:
            self.logger.info(f"Exclude keywords: {self.exclude_keywords}", filename="SYSTEM")
        self.logger.info(f"Normalization   : {'ON' if normalize_sku else 'OFF'}", filename="SYSTEM")
        self.logger.info(f"Case sensitive  : {'YES' if case_sensitive else 'NO'}", filename="SYSTEM")
        self.logger.info(
            "Supported formats: CSV, Excel (.xlsx/.xls), XML, ZIP (containing CSV/Excel/XML)",
            filename="SYSTEM",
        )
        if not self.folder1.exists():
            raise FileNotFoundError(f"Folder not found: {self.folder1}")
        if not self.folder2.exists():
            raise FileNotFoundError(f"Folder not found: {self.folder2}")
        if self.temp_diff_dir is None:
            self.temp_diff_dir = Path(tempfile.mkdtemp(prefix="feed_validator_diffs_"))
        else:
            self.temp_diff_dir = Path(self.temp_diff_dir)
            self.temp_diff_dir.mkdir(parents=True, exist_ok=True)
        self.matching_files = self._find_files()
        self.logger.info(f"Matching file pairs: {len(self.matching_files)}", filename="SYSTEM")
        for f in self.matching_files:
            self.logger.debug(f"  📄 {f}", filename="SYSTEM")

    def _find_files(self):
        if self.file_pattern:
            f1 = {f.name for f in self.folder1.glob(self.file_pattern) if f.is_file()}
            f2 = {f.name for f in self.folder2.glob(self.file_pattern) if f.is_file()}
        else:
            f1 = {
                f.name for f in self.folder1.iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
            }
            f2 = {
                f.name for f in self.folder2.iterdir()
                if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS
            }
        only_f1 = f1 - f2
        only_f2 = f2 - f1
        if only_f1:
            self.logger.warn(f"Files only in baseline ({len(only_f1)})", filename="SYSTEM")
            for f in sorted(only_f1)[:5]:
                self.logger.warn(f"  - {f}", filename="SYSTEM")
        if only_f2:
            self.logger.warn(f"Files only in comparison ({len(only_f2)})", filename="SYSTEM")
            for f in sorted(only_f2)[:5]:
                self.logger.warn(f"  - {f}", filename="SYSTEM")
        return sorted(f1 & f2)

    def _read_file(self, file_path: Path, filename: str) -> pl.DataFrame:
        """Delegate to FileReader; filename is threaded through for log context."""
        return self.file_reader._read_file(file_path, filename)

    # ------------------------------------------------------------------
    # SKU handling
    # ------------------------------------------------------------------

    def _normalized_column(self, df: pl.DataFrame, column: str) -> pl.Series:
        """Build the join key for ``column`` using the configured rules.

        The same rule must produce both the comparison key and the join key.
        The previous implementation derived the *sets* from a lightly-cleaned
        string while deriving ``_sku_norm`` from the fully normalized one, so
        with normalization disabled the inner join silently dropped every row.
        """
        if self.normalize_sku:
            series = self.normalizer.normalize_series(df[column])
        else:
            series = df[column].cast(pl.String, strict=False).fill_null('').str.strip_chars()
        if not self.case_sensitive:
            series = series.str.to_lowercase()
        return series

    @staticmethod
    def _sku_maps(df: pl.DataFrame, raw_col: str) -> dict:
        """Map normalized SKU -> first raw representation, skipping blanks."""
        pairs = (
            df.select([raw_col, '_sku_norm'])
            .filter(pl.col('_sku_norm') != '')
            .unique(subset=['_sku_norm'], keep='first')
        )
        return dict(zip(pairs['_sku_norm'].to_list(), pairs[raw_col].to_list()))

    def _align_dataframes(self, df1, df2, filename):
        """Row-align two DataFrames on the shared normalized SKU set.

        Both outputs are produced by joining the *same* sorted key frame, so
        positional alignment (relied on by ColumnMatchStats) is guaranteed by
        construction rather than by two independent sorts agreeing.

        Blank SKUs are excluded from the key frame. They would otherwise
        inner-join to each other and be compared as if they were one shared
        product row.
        """
        dup1 = df1.height - df1['_sku_norm'].n_unique()
        dup2 = df2.height - df2['_sku_norm'].n_unique()
        if dup1 > 0 or dup2 > 0:
            self.logger.debug(
                f"  Duplicate SKUs: F1={dup1}, F2={dup2}. Keeping first.",
                filename=filename,
            )

        u1 = df1.unique(subset=['_sku_norm'], keep='first')
        u2 = df2.unique(subset=['_sku_norm'], keep='first')

        keys = (
            u1.select('_sku_norm')
            .join(u2.select('_sku_norm'), on='_sku_norm', how='inner')
            .filter(pl.col('_sku_norm') != '')
            .sort('_sku_norm')
        )

        df1_common = keys.join(u1, on='_sku_norm', how='left', maintain_order='left')
        df2_common = keys.join(u2, on='_sku_norm', how='left', maintain_order='left')

        self.logger.debug(
            f"  Aligned: {df1_common.height} common rows",
            filename=filename,
        )
        return df1_common, df2_common

    # ------------------------------------------------------------------
    # Per-pair validation
    # ------------------------------------------------------------------

    def _resolve_sku_columns(self, df1, df2, filename):
        """Return (sku_col1, sku_col2) or a skip/error result dict."""
        if self.auto_detect_sku:
            self.logger.detect("🔍 Auto-detecting SKU column...", filename=filename)
            if self.user_sku_column:
                if self.user_sku_column in df1.columns and self.user_sku_column in df2.columns:
                    self.logger.detect(
                        f"✅ Using user-specified: '{self.user_sku_column}'", filename=filename,
                    )
                    return self.user_sku_column, self.user_sku_column, None
            sku_col1, sku_col2, confidence = self.sku_detector.detect_common_sku(
                df1.columns, df2.columns, filename,
            )
            if sku_col1 is None:
                self.logger.skip("⏭️ SKIPPED: No SKU column detected", filename=filename)
                return None, None, {
                    'status': 'skipped', 'file': filename,
                    'reason': 'No SKU column detected',
                }
            if confidence < 0.7:
                self.logger.warn(f"⚠️ Low confidence ({confidence:.2f})", filename=filename)
            return sku_col1, sku_col2, None

        if not self.user_sku_column:
            return None, None, {
                'status': 'error', 'file': filename,
                'error': 'No SKU column specified',
            }
        if (self.user_sku_column not in df1.columns
                or self.user_sku_column not in df2.columns):
            return None, None, {
                'status': 'skipped', 'file': filename,
                'reason': f"SKU column '{self.user_sku_column}' not found",
            }
        return self.user_sku_column, self.user_sku_column, None

    def _select_compare_columns(self, df1, df2, sku_col1, sku_col2, filename):
        if self.compare_columns:
            valid_cols = [
                c for c in self.compare_columns
                if c in df1.columns and c in df2.columns
            ]
            missing = [c for c in self.compare_columns if c not in valid_cols]
            if missing:
                self.logger.warn(
                    f"⚠️ Requested columns not present in both files: {missing}",
                    filename=filename,
                )
        else:
            common_cols = (set(df1.columns) & set(df2.columns)) - {
                sku_col1, sku_col2, '_sku_norm',
            }
            valid_cols = sorted(common_cols)

        if self.exclude_keywords:
            excluded_cols = [
                c for c in valid_cols
                if any(kw.lower() in c.lower() for kw in self.exclude_keywords)
            ]
            if excluded_cols:
                valid_cols = [c for c in valid_cols if c not in excluded_cols]
                self.logger.debug(f"  Excluded columns: {excluded_cols}", filename=filename)

        return valid_cols

    def _diff_path(self, filename: str) -> Path:
        safe_name = re.sub(r'[^\w\-]', '_', filename)[:60]
        digest = hashlib.sha1(filename.encode('utf-8')).hexdigest()[:8]
        return self.temp_diff_dir / f"diff_{safe_name}_{digest}.csv"

    def _validate_single_pair(self, filename):
        file1_path = self.folder1 / filename
        file2_path = self.folder2 / filename
        self.logger.info("=" * 70, filename=filename)
        self.logger.info("📄 START PROCESSING", filename=filename)
        self.logger.info(f"  Baseline : {file1_path.name} ({file1_path.suffix.upper()})", filename=filename)
        self.logger.info(f"  Comparison: {file2_path.name} ({file2_path.suffix.upper()})", filename=filename)
        try:
            df1 = self._read_file(file1_path, filename)
            df2 = self._read_file(file2_path, filename)
            rows_file1, rows_file2 = df1.height, df2.height
            self.logger.info(f"  File 1: {rows_file1:,} rows, {df1.width} cols", filename=filename)
            self.logger.info(f"  File 2: {rows_file2:,} rows, {df2.width} cols", filename=filename)

            sku_col1, sku_col2, abort = self._resolve_sku_columns(df1, df2, filename)
            if abort is not None:
                return abort
            self.logger.match(f"🎯 SKU column File1: '{sku_col1}'", filename=filename)
            self.logger.match(f"🎯 SKU column File2: '{sku_col2}'", filename=filename)

            df1 = df1.with_columns(
                pl.col(sku_col1).cast(pl.String, strict=False).alias(sku_col1)
            )
            df2 = df2.with_columns(
                pl.col(sku_col2).cast(pl.String, strict=False).alias(sku_col2)
            )

            df1 = df1.with_columns(
                self._normalized_column(df1, sku_col1).alias('_sku_norm')
            )
            df2 = df2.with_columns(
                self._normalized_column(df2, sku_col2).alias('_sku_norm')
            )

            norm_map1 = self._sku_maps(df1, sku_col1)
            norm_map2 = self._sku_maps(df2, sku_col2)
            norm_set1 = set(norm_map1)
            norm_set2 = set(norm_map2)

            common_norm = norm_set1 & norm_set2
            only_1 = norm_set1 - norm_set2
            only_2 = norm_set2 - norm_set1

            if not common_norm:
                self.logger.warn(
                    "⚠️ No common SKUs between the two files", filename=filename,
                )

            df1_common, df2_common = self._align_dataframes(df1, df2, filename)

            valid_cols = self._select_compare_columns(
                df1, df2, sku_col1, sku_col2, filename,
            )
            if not valid_cols:
                self.logger.warn(
                    "⚠️ No comparable columns; every column was excluded or absent",
                    filename=filename,
                )

            diff_csv = self._diff_path(filename)
            column_stats = ColumnMatchStats.calculate(
                df1_common, df2_common, sku_col1, sku_col2, valid_cols, str(diff_csv),
            )

            total_diffs = sum(s['mismatches'] for s in column_stats.values())
            avg_match_rate = (
                sum(s['match_rate'] for s in column_stats.values()) / len(column_stats)
                if column_stats else 0
            )

            # Release the frames before returning; the summary below only
            # needs the scalars computed above.
            del df1, df2, df1_common, df2_common

            return {
                'status': 'success', 'file': filename,
                'sku_col1': sku_col1, 'sku_col2': sku_col2,
                'summary': {
                    'rows_file1': rows_file1, 'rows_file2': rows_file2,
                    'unique_skus_file1': len(norm_set1),
                    'unique_skus_file2': len(norm_set2),
                    'common_skus': len(common_norm),
                    'only_in_file1': len(only_1),
                    'only_in_file2': len(only_2),
                    'total_differences': total_diffs,
                    'avg_match_rate': round(avg_match_rate, 1),
                    'columns_compared': len(valid_cols),
                },
                'column_stats': column_stats, 'diff_csv_path': str(diff_csv),
                'missing_in_file1': sorted(norm_map2.get(s, s) for s in only_2),
                'missing_in_file2': sorted(norm_map1.get(s, s) for s in only_1),
            }
        except Exception as e:
            self.logger.error(f"❌ ERROR: {str(e)}", filename=filename)
            self.logger.debug(f"  Traceback: {traceback.format_exc()}", filename=filename)
            return {'status': 'error', 'file': filename, 'error': str(e)}

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def validate_all_parallel(self, progress_callback=None,
                              cancel_event: Optional[threading.Event] = None,
                              progress_base: float = 0.0,
                              progress_span: float = 1.0):
        """Run all matching pairs in a bounded thread pool.

        If ``cancel_event`` is set while results are still streaming in, the
        coordinator stops submitting new pairs and returns what has been
        collected. Already-running pair validations finish for consistency.

        ``progress_base`` / ``progress_span`` place this phase inside the
        overall 0..1 progress bar. A download-and-compare run reserves the
        first 60% for downloading and reports base=0.6, span=0.4 here; file
        mode has no download phase and uses the whole bar.
        """
        def _is_cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        total = len(self.matching_files)
        completed = 0
        self.logger.info(
            f"🚀 STARTING VALIDATION: {total} files, {self.max_workers} workers",
            filename="SYSTEM",
        )
        all_results = {
            'files': {}, 'errors': [], 'skipped': [],
            'overall': {
                'total_pairs': total, 'success': 0, 'failed': 0, 'skipped': 0,
                'total_skus': 0, 'total_diffs': 0, 'total_missing': 0, 'avg_match_rate': 0,
            },
            'overall_column_stats': {}, 'debug_logs': [], 'sku_detection_log': [],
            'temp_diff_dir': str(self.temp_diff_dir),
        }
        all_column_stats = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_file = {
                executor.submit(self._validate_single_pair, f): f
                for f in self.matching_files
            }
            for future in as_completed(future_to_file):
                if _is_cancelled():
                    for f in future_to_file:
                        f.cancel()
                    break

                filename = future_to_file[future]
                completed += 1
                try:
                    result = future.result()
                except Exception as e:
                    result = {'status': 'error', 'file': filename, 'error': str(e)}

                all_results['files'][filename] = result
                if result['status'] == 'success':
                    all_results['overall']['success'] += 1
                    s = result['summary']
                    all_results['overall']['total_skus'] += s['common_skus']
                    all_results['overall']['total_diffs'] += s['total_differences']
                    all_results['overall']['total_missing'] += (
                        s['only_in_file1'] + s['only_in_file2']
                    )
                    all_column_stats.append(result['column_stats'])
                elif result['status'] == 'skipped':
                    all_results['overall']['skipped'] += 1
                    all_results['skipped'].append(
                        {'file': filename, 'reason': result.get('reason', 'Unknown')}
                    )
                else:
                    all_results['overall']['failed'] += 1
                    all_results['errors'].append(
                        {'file': filename, 'error': result.get('error', 'Unknown')}
                    )

                if progress_callback:
                    progress_callback(ProgressUpdate(
                        phase=PHASE_VALIDATE,
                        index=completed,
                        total=total,
                        label=filename,
                        fraction=(completed / total) if total else 0.0,
                        base=progress_base,
                        span=progress_span,
                    ))

        if all_column_stats:
            all_results['overall_column_stats'] = ColumnMatchStats.calculate_overall(all_column_stats)
            rates = [s['match_rate'] for s in all_results['overall_column_stats'].values()]
            all_results['overall']['avg_match_rate'] = round(sum(rates) / len(rates), 1) if rates else 0
        all_results['debug_logs'] = self.logger.get_logs()
        all_results['log_summary'] = self.logger.get_summary()
        all_results['sku_detection_log'] = self.sku_detector.get_log()
        return all_results

    def cleanup(self):
        """Remove the temp diff directory. Safe to call multiple times."""
        if self.temp_diff_dir and Path(self.temp_diff_dir).exists():
            shutil.rmtree(self.temp_diff_dir, ignore_errors=True)
