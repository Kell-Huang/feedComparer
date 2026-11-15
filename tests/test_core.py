"""Core-layer tests: readers, normalizer, diff engine, validator.

Assertions are exact counts, not "something happened", so a regression in the
diff semantics fails loudly. Expected numbers are documented next to each
check and mirror the constants in ``make_test_data.py``.
"""

import tempfile
from pathlib import Path

import make_test_data as gen
import polars as pl

from core.logger import DebugLogger
from core.normalizer import SKUNormalizer
from core.reader import FileReader
from core.sku_detector import SKUColumnDetector
from core.stats import ColumnMatchStats
from core.validator import FastBatchValidator

DATA = Path(__file__).resolve().parent / "data"
SITE_A = DATA / gen.SITE_A
SITE_B = DATA / gen.SITE_B


def _ensure_data():
    if not SITE_A.exists() or not SITE_B.exists():
        gen.build(DATA)


def _validate(**kwargs):
    _ensure_data()
    kwargs.setdefault("exclude_keywords", [])
    kwargs.setdefault("debug", True)
    validator = FastBatchValidator(str(SITE_A), str(SITE_B), None, [], **kwargs)
    try:
        return validator.validate_all_parallel()
    finally:
        validator.cleanup()


# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------


def test_normalizer_vectorized_matches_reference():
    """The fast path must produce byte-identical output to the reference."""
    normalizer = SKUNormalizer()
    cases = [
        "0012",
        "12",
        "12.0",
        "12.",
        "0.0",
        "-12",
        "-0",
        "1.2.3",
        "12.50",
        "  7  ",
        "A\u00a0B",
        "１００",
        "",
        "0",
        "00",
        "-12.0",
        "abc",
        "12 34",
        "000",
        "1000",
        "-0012",
        "1,234",
        "a\u2013b",
        None,
    ]
    series = pl.Series("sku", cases, dtype=pl.String)
    vectorized = normalizer.normalize_series(series).to_list()
    reference = [normalizer.normalize(c) for c in cases]
    assert vectorized == reference, list(zip(cases, vectorized, reference))


def test_normalizer_expected_values():
    normalizer = SKUNormalizer()
    assert normalizer.normalize("0012") == "12"
    assert normalizer.normalize("12.0") == "12"
    assert normalizer.normalize("12.50") == "12.50"
    assert normalizer.normalize("1.2.3") == "1.2.3"
    assert normalizer.normalize(None) == ""
    assert normalizer.normalize(float("nan")) == ""
    assert normalizer.normalize("１００") == "100"


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------


def test_reader_handles_every_format():
    _ensure_data()
    reader = FileReader(DebugLogger())

    csv_df = reader._read_file(SITE_A / "feed_main.csv", "feed_main.csv")
    assert csv_df.height == gen.MAIN_SKUS_A, csv_df.height
    assert csv_df.columns == [
        "sku",
        "title",
        "price",
        "stock",
        "currency",
        "updated_at",
    ]

    # GBK must be transcoded, not mojibake'd.
    gbk_df = reader._read_file(SITE_A / "feed_gbk.csv", "feed_gbk.csv")
    assert gbk_df.columns[1] == "名称", gbk_df.columns
    assert gbk_df["名称"].to_list() == ["商品一", "商品二"]

    # Delimiter detection must not be fooled by the embedded comma.
    semi_df = reader._read_file(SITE_A / "feed_semicolon.csv", "feed_semicolon.csv")
    assert semi_df.columns == ["sku", "name", "qty"], semi_df.columns
    assert semi_df["name"].to_list()[0] == "Widget, large"

    pipe_df = reader._read_file(SITE_A / "feed_pipe.csv", "feed_pipe.csv")
    assert pipe_df.columns == ["sku", "name", "qty"], pipe_df.columns

    # Namespaced XML: g:id / g:price must lose their prefix.
    xml_df = reader._read_file(SITE_A / "feed_xml.xml", "feed_xml.xml")
    assert set(xml_df.columns) == {"id", "title", "price"}, xml_df.columns
    assert xml_df.height == 3

    # Malformed XML: recover parser must still find both items.
    broken_df = reader._read_file(SITE_A / "feed_broken.xml", "feed_broken.xml")
    assert broken_df.height == 2, broken_df.height

    # Excel with null cells used to raise on fill_null("").
    xlsx_df = reader._read_file(SITE_A / "feed_excel.xlsx", "feed_excel.xlsx")
    assert xlsx_df.height == 5, xlsx_df.height
    assert xlsx_df["note"].to_list()[-1] == ""

    zip_df = reader._read_file(SITE_A / "feed_zip.zip", "feed_zip.zip")
    assert zip_df.columns == ["sku", "price"], zip_df.columns


def test_reader_column_discovery_is_header_only():
    _ensure_data()
    reader = FileReader(DebugLogger())
    for name, expected in [
        ("feed_main.csv", ["sku", "title", "price", "stock", "currency", "updated_at"]),
        ("feed_excel.xlsx", ["sku", "price", "note"]),
        ("feed_zip.zip", ["sku", "price"]),
    ]:
        columns = reader.read_columns(SITE_A / name)
        assert columns == expected, (name, columns)


def test_reader_csv_values_are_strings():
    """Values must survive as text so 'N/A' cannot be coerced to null."""
    _ensure_data()
    reader = FileReader(DebugLogger())
    df = reader._read_file(SITE_A / "feed_main.csv", "feed_main.csv")
    assert df.schema["price"] == pl.String, df.schema


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------


def test_values_equal_semantics():
    eq = ColumnMatchStats._values_equal
    assert eq("100", "100.0")
    assert eq("1,234.50", "1234.5")
    assert eq("１００", "100")
    assert eq("2024-01-01", "2024/01/01")
    assert eq("true", "TRUE")
    assert eq("0.0", "0")
    assert not eq("abc", "ABC")
    assert not eq("12", "13")
    assert not eq("", "x")


def test_stats_counters_are_consistent():
    df1 = pl.DataFrame({"_sku_norm": ["a", "b", "c"], "v": ["1", "", "x"]})
    df2 = pl.DataFrame({"_sku_norm": ["a", "b", "c"], "v": ["1.0", "y", "x"]})
    with tempfile.TemporaryDirectory() as tmp:
        stats = ColumnMatchStats.calculate(
            df1, df2, "sku", "sku", ["v"], str(Path(tmp) / "d.csv")
        )
        s = stats["v"]
        assert s["total_compared"] == 3
        assert s["exact_matches"] + s["mismatches"] == s["total_compared"]
        assert s["exact_matches"] == 2  # '1'=='1.0', 'x'=='x'
        assert s["mismatches"] == 1  # '' vs 'y'
        assert s["only_file1_nan"] == 1


# ---------------------------------------------------------------------------
# SKU detector
# ---------------------------------------------------------------------------


def test_detector_rejects_unrelated_columns():
    detector = SKUColumnDetector()
    assert detector.detect_sku_column(["name", "price"])[0] is None
    assert detector.detect_sku_column(["sku", "name"])[0] == "sku"
    assert detector.detect_sku_column(["g:id", "title"])[0] == "g:id"


def test_detector_excludes_known_non_sku_ids():
    detector = SKUColumnDetector()
    assert detector.detect_sku_column(["category_id", "ean"])[0] == "ean"


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------


def test_logger_is_bounded():
    logger = DebugLogger(max_entries=10)
    for i in range(25):
        logger.info(f"entry {i}")
    assert len(logger.get_logs()) == 10
    assert logger.dropped == 15
    assert logger.get_summary()["DROPPED"] == 15


def test_logger_keeps_errors_when_debug_disabled():
    logger = DebugLogger()
    logger.enabled = False
    logger.info("hidden")
    logger.error("visible")
    messages = [entry["message"] for entry in logger.get_logs()]
    assert messages == ["visible"], messages


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------


def test_validator_pairs_and_overall():
    results = _validate()
    assert results["overall"]["total_pairs"] == 11, results["overall"]
    assert results["overall"]["success"] == 10
    assert results["overall"]["skipped"] == 1
    assert results["overall"]["failed"] == 0


def test_validator_main_file_exact_counts():
    results = _validate()
    result = results["files"]["feed_main.csv"]
    assert result["status"] == "success"
    assert result["sku_col1"] == "sku"

    summary = result["summary"]
    assert summary["rows_file1"] == gen.MAIN_SKUS_A
    assert summary["rows_file2"] == gen.MAIN_SKUS_B
    assert summary["common_skus"] == gen.MAIN_COMMON
    assert summary["only_in_file1"] == gen.MAIN_ONLY_A
    assert summary["only_in_file2"] == gen.MAIN_ONLY_B
    assert summary["total_differences"] == gen.MAIN_TOTAL_DIFFS
    assert summary["columns_compared"] == 5

    stats = result["column_stats"]
    assert stats["price"]["mismatches"] == 5
    assert stats["title"]["mismatches"] == 1
    assert stats["stock"]["mismatches"] == 1
    assert stats["stock"]["only_file2_nan"] == 1
    assert stats["currency"]["match_rate"] == 100
    assert stats["updated_at"]["match_rate"] == 100

    # Missing SKUs are reported using the original representation from the
    # file they came from, so a zero-padded source stays zero-padded.
    assert result["missing_in_file2"] == ["00036", "00037", "00038"]
    assert result["missing_in_file1"] == ["39", "40"]


def test_validator_diff_csv_contents():
    _ensure_data()
    validator = FastBatchValidator(
        str(SITE_A), str(SITE_B), None, [], exclude_keywords=[]
    )
    try:
        results = validator.validate_all_parallel()
        path = Path(results["files"]["feed_main.csv"]["diff_csv_path"])
        assert path.exists(), path
        lines = path.read_text(encoding="utf-8-sig").strip().splitlines()
        assert lines[0] == "sku,column,value_file1,value_file2"
        assert len(lines) - 1 == gen.MAIN_TOTAL_DIFFS, len(lines) - 1
    finally:
        validator.cleanup()


def test_validator_numeric_equivalence():
    results = _validate()
    result = results["files"]["feed_numeric.csv"]
    assert result["summary"]["common_skus"] == 7
    # Only 'abc' vs 'ABC' is a genuine difference.
    assert result["summary"]["total_differences"] == 1, result["summary"]


def test_validator_skips_file_without_sku_column():
    results = _validate()
    result = results["files"]["feed_nosku.csv"]
    assert result["status"] == "skipped", result


def test_validator_duplicate_skus_keep_first():
    results = _validate()
    result = results["files"]["feed_dup.csv"]
    # Duplicate '1' (10 then 999) collapses to the first row, which matches.
    assert result["summary"]["common_skus"] == 2
    assert result["summary"]["total_differences"] == 0


def test_validator_exclude_keywords():
    results = _validate(exclude_keywords=["stock", "price"])
    result = results["files"]["feed_main.csv"]
    assert result["summary"]["columns_compared"] == 3
    assert result["summary"]["total_differences"] == 1  # title only


def test_validator_explicit_compare_columns():
    _ensure_data()
    validator = FastBatchValidator(
        str(SITE_A), str(SITE_B), None, ["price"], exclude_keywords=[]
    )
    try:
        results = validator.validate_all_parallel()
    finally:
        validator.cleanup()
    result = results["files"]["feed_main.csv"]
    assert result["summary"]["columns_compared"] == 1
    assert result["summary"]["total_differences"] == 5


def test_validator_case_sensitivity_applies_to_sku_matching():
    """``case_sensitive`` governs SKU keys, not cell values.

    With it enabled 'ABC' and 'abc' are different products and nothing lines
    up; with it disabled they merge into one row.
    """
    with tempfile.TemporaryDirectory() as tmp:
        left = Path(tmp) / "a"
        right = Path(tmp) / "b"
        left.mkdir()
        right.mkdir()
        (left / "f.csv").write_text("sku,price\nABC,10\n", encoding="utf-8")
        (right / "f.csv").write_text("sku,price\nabc,10\n", encoding="utf-8")

        strict = FastBatchValidator(
            str(left), str(right), None, [], exclude_keywords=[], case_sensitive=True
        )
        try:
            strict_result = strict.validate_all_parallel()["files"]["f.csv"]
        finally:
            strict.cleanup()
        assert strict_result["summary"]["common_skus"] == 0, strict_result["summary"]

        loose = FastBatchValidator(
            str(left), str(right), None, [], exclude_keywords=[], case_sensitive=False
        )
        try:
            loose_result = loose.validate_all_parallel()["files"]["f.csv"]
        finally:
            loose.cleanup()
        assert loose_result["summary"]["common_skus"] == 1, loose_result["summary"]
        assert loose_result["summary"]["total_differences"] == 0


def test_validator_without_normalization_keeps_join_key_consistent():
    """Regression: the join key used to be normalized even when disabled.

    With normalization off the comparison sets came from a lightly cleaned
    string while ``_sku_norm`` came from the fully normalized one, so the
    inner join dropped every row and the file silently reported 0 common SKUs.
    """
    with tempfile.TemporaryDirectory() as tmp:
        left = Path(tmp) / "a"
        right = Path(tmp) / "b"
        left.mkdir()
        right.mkdir()
        for folder in (left, right):
            (folder / "f.csv").write_text(
                "sku,price\n0012,10\n0013,20\n",
                encoding="utf-8",
            )
        validator = FastBatchValidator(
            str(left), str(right), None, [], exclude_keywords=[], normalize_sku=False
        )
        try:
            results = validator.validate_all_parallel()
        finally:
            validator.cleanup()
        result = results["files"]["f.csv"]
        assert result["status"] == "success", result
        assert result["summary"]["common_skus"] == 2, result["summary"]
        assert result["summary"]["total_differences"] == 0


def test_validator_cancellation_returns_cancelled():
    import threading

    _ensure_data()
    event = threading.Event()
    event.set()
    validator = FastBatchValidator(
        str(SITE_A), str(SITE_B), None, [], exclude_keywords=[]
    )
    try:
        results = validator.validate_all_parallel(cancel_event=event)
    finally:
        validator.cleanup()
    assert results["overall"]["success"] == 0, results["overall"]
    assert results["overall"]["total_pairs"] == 11


def test_validator_missing_folder_raises():
    try:
        FastBatchValidator("does/not/exist", "nor/this", None, [])
    except FileNotFoundError:
        return
    raise AssertionError("expected FileNotFoundError")


def test_validator_temp_dir_is_not_leaked_on_bad_constructor():
    """A failed constructor must not leave an empty mkdtemp behind."""
    import glob
    import tempfile

    before = set(glob.glob(str(Path(tempfile.gettempdir()) / "feed_validator_diffs_*")))
    try:
        FastBatchValidator("does/not/exist", "nor/this", None, [])
    except FileNotFoundError:
        pass
    after = set(glob.glob(str(Path(tempfile.gettempdir()) / "feed_validator_diffs_*")))
    assert after == before, after - before


def test_diff_export_can_be_skipped():
    """With no path the comparison still runs; only the file is skipped."""
    import tempfile

    import polars as pl

    from core.stats import ColumnMatchStats

    left = pl.DataFrame(
        {
            "sku": ["A", "B"],
            "_sku_norm": ["a", "b"],
            "title": ["x", "y"],
            "price": ["1", "2"],
        }
    )
    right = pl.DataFrame(
        {
            "sku": ["A", "B"],
            "_sku_norm": ["a", "b"],
            "title": ["x", "z"],
            "price": ["1", "9"],
        }
    )

    with tempfile.TemporaryDirectory() as tmp:
        with_file = Path(tmp) / "diff.csv"
        written = ColumnMatchStats.calculate(
            left, right, "sku", "sku", ["title", "price"], str(with_file)
        )
        skipped = ColumnMatchStats.calculate(
            left, right, "sku", "sku", ["title", "price"], None
        )

        assert written == skipped  # same numbers either way
        assert written["title"]["mismatches"] == 1
        assert written["price"]["mismatches"] == 1
        assert with_file.exists()
        assert list(Path(tmp).iterdir()) == [with_file]  # nothing else created


def test_diff_file_always_has_a_header():
    """A run with no differences still leaves a readable file."""
    import tempfile

    import polars as pl

    from core.stats import ColumnMatchStats

    left = pl.DataFrame({"sku": ["A"], "_sku_norm": ["a"], "title": ["x"]})
    right = left.clone()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "same.csv"
        ColumnMatchStats.calculate(left, right, "sku", "sku", ["title"], str(path))
        assert path.read_text(encoding="utf-8-sig").splitlines() == [
            "sku,column,value_file1,value_file2"
        ]


def test_validator_can_skip_the_diff_export():
    from core.validator import FastBatchValidator

    data = Path(__file__).resolve().parent / "data"
    if not (data / gen.SITE_A).exists():
        gen.build(data)

    validator = FastBatchValidator(
        str(data / gen.SITE_A),
        str(data / gen.SITE_B),
        None,
        [],
        exclude_keywords=[],
        export_diff=False,
    )
    try:
        results = validator.validate_all_parallel()
    finally:
        validator.cleanup()

    for name, result in results["files"].items():
        if result["status"] != "success":
            continue
        assert result["diff_csv_path"] == "", name
        # The numbers are still produced, which is the point of the switch.
        assert result["column_stats"], name
    # And the overall aggregation is unaffected.
    assert results["overall_column_stats"]


def test_date_gate_accepts_exactly_what_strptime_accepts():
    """The shape gate must not change which strings count as dates."""
    from datetime import datetime

    from core.stats import ColumnMatchStats as S

    accepted = [
        "2024-01-05",
        "2024/01/05",
        "2024-1-5",
        "2024-01-05 10:00:00",
        "2024-01-05T10:00:00",
        "2024-01-05T10:00:00.123456",
    ]
    for value in accepted:
        assert S._looks_like_date(value, allow_compact=False), value
        assert S._to_datetime(value, allow_compact=False) is not None, value

    rejected = [
        "Blue T-Shirt",
        "SKU-2024",
        "2024",
        "",
        "01/05/2024",
        "999-01-01",
    ]  # %Y needs four digits
    for value in rejected:
        assert not S._looks_like_date(value, allow_compact=False), value
        assert S._to_datetime(value, allow_compact=False) is None, value

    # Compact dates only when the other side is already a real date.
    assert not S._looks_like_date("20240105", allow_compact=False)
    assert S._to_datetime("20240105", allow_compact=False) is None
    assert S._to_datetime("20240105", allow_compact=True) == datetime(2024, 1, 5)


def test_equivalence_still_holds_after_the_date_gate():
    from core.stats import ColumnMatchStats as S

    assert S._values_equal("2024-01-05", "2024/01/05")
    assert S._values_equal("2024-01-05 10:00:00", "2024-01-05T10:00:00")
    assert not S._values_equal("2024-01-05", "2024-01-06")
    assert not S._values_equal("Blue T-Shirt", "Red Hoodie")
    assert S._values_equal("11.00", "11")
