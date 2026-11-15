"""Large-batch and hard-edge-case test data.

Everything is generated into a temporary directory at runtime and removed on
exit, so a multi-hundred-megabyte fixture never lands in the repository.

Content is deterministic: the number of differences is a closed-form function
of the row count, so the tests can assert exact counts instead of "something
was found".

Layout produced under the temp root:

    big/       150k rows x 20 columns          (core diff at scale)
    wide/       40k rows x 120 columns         (column explosion)
    many/       40 pairs x 2k rows             (concurrency + aggregation)
    xml/        10k <item> elements            (XML at scale)
    xlsx/       15k rows                       (Excel at scale)
    zip/        80k rows inside a .zip         (archive at scale)
    edge/       header-only, BOM/CRLF, reordered and extra columns,
                all-null SKUs, unicode + emoji values
"""

import atexit
import csv
import shutil
import tempfile
import zipfile
from pathlib import Path

from openpyxl import Workbook

BIG_ROWS = 150_000
BIG_COLS = 20
BIG_ONLY_A = 100
BIG_ONLY_B = 50
BIG_COMMON = BIG_ROWS - BIG_ONLY_A

WIDE_ROWS = 40_000
WIDE_ATTRS = 118

MANY_PAIRS = 40
MANY_ROWS = 2_000

XML_ITEMS = 10_000
XLSX_ROWS = 15_000
ZIP_ROWS = 80_000

_ROOT = None


# ---------------------------------------------------------------------------
# Expected values (kept next to the generator so tests cannot drift)
# ---------------------------------------------------------------------------


def big_expected():
    """Exact expectations for the big pair."""
    # price differs on every 1000th SKU, title on every 5000th.
    price_diffs = BIG_COMMON // 1000
    title_diffs = BIG_COMMON // 5000
    return {
        "rows_file1": BIG_ROWS,
        "rows_file2": BIG_ROWS - BIG_ONLY_A + BIG_ONLY_B,
        "common_skus": BIG_COMMON,
        "only_in_file1": BIG_ONLY_A,
        "only_in_file2": BIG_ONLY_B,
        "price_diffs": price_diffs,
        "title_diffs": title_diffs,
        "total_diffs": price_diffs + title_diffs,
        "columns_compared": BIG_COLS - 1,  # sku is the key
    }


def _write_csv(path, header, rows, encoding="utf-8", newline=""):
    with open(path, "w", encoding=encoding, newline=newline) as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------


def _build_big(root: Path):
    folder = root / "big"
    a_dir, b_dir = folder / "a", folder / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)

    header = ["sku", "title", "price", "stock", "currency", "note"]
    header += [f"attr_{i}" for i in range(1, BIG_COLS - 5)]

    def rows(side):
        if side == "a":
            skus = range(1, BIG_ROWS + 1)
        else:
            skus = list(range(1, BIG_COMMON + 1)) + [
                BIG_ROWS + i for i in range(1, BIG_ONLY_B + 1)
            ]
        out = []
        for sku in skus:
            title = f"Product {sku}"
            price = 10.0 + (sku % 500)
            if side == "b" and sku % 1000 == 0:
                price += 1.5
            if side == "b" and sku % 5000 == 0:
                title = f"product {sku}"
            # A sparse column: both sides empty on 9 of 10 rows.
            note = f"note {sku}" if sku % 10 == 0 else ""
            row = [sku, title, f"{price:.2f}", str(sku % 50), "USD", note]
            row += [f"v{sku % 97}_{i}" for i in range(1, BIG_COLS - 5)]
            out.append(row)
        return out

    _write_csv(a_dir / "feed.csv", header, rows("a"))
    _write_csv(b_dir / "feed.csv", header, rows("b"))


def _build_wide(root: Path):
    folder = root / "wide"
    a_dir, b_dir = folder / "a", folder / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    header = ["sku"] + [f"attr_{i}" for i in range(1, WIDE_ATTRS + 1)]

    def rows(side):
        out = []
        for sku in range(1, WIDE_ROWS + 1):
            row = [sku]
            for i in range(1, WIDE_ATTRS + 1):
                value = f"v{i}_{sku % 13}"
                if side == "b" and i == 7 and sku % 2000 == 0:
                    value = f"changed_{sku}"
                row.append(value)
            out.append(row)
        return out

    _write_csv(a_dir / "wide.csv", header, rows("a"))
    _write_csv(b_dir / "wide.csv", header, rows("b"))


def _build_many(root: Path):
    folder = root / "many"
    a_dir, b_dir = folder / "a", folder / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)
    header = ["sku", "price", "stock"]
    for pair in range(1, MANY_PAIRS + 1):
        a_rows, b_rows = [], []
        for sku in range(1, MANY_ROWS + 1):
            a_rows.append([sku, f"{10 + sku % 100:.2f}", str(sku % 20)])
            price = 10 + sku % 100
            if pair % 2 == 0 and sku % 500 == 0:
                price += 1
            b_rows.append([sku, f"{price:.2f}", str(sku % 20)])
        name = f"feed_{pair:02d}.csv"
        _write_csv(a_dir / name, header, a_rows)
        _write_csv(b_dir / name, header, b_rows)


def _build_xml(root: Path):
    folder = root / "xml"
    a_dir, b_dir = folder / "a", folder / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)

    def body(side):
        parts = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<rss xmlns:g="http://base.google.com/ns/1.0"><channel>',
        ]
        for idx in range(1, XML_ITEMS + 1):
            price = 10 + idx % 200
            if side == "b" and idx % 1000 == 0:
                price += 1
            parts.append(
                f"<item><g:id>{idx}</g:id><title>Item {idx}</title>"
                f"<g:price>{price}.00</g:price></item>"
            )
        parts.append("</channel></rss>")
        return "\n".join(parts)

    (a_dir / "feed.xml").write_text(body("a"), encoding="utf-8")
    (b_dir / "feed.xml").write_text(body("b"), encoding="utf-8")


def _build_xlsx(root: Path):
    folder = root / "xlsx"
    for side in ("a", "b"):
        target = folder / side
        target.mkdir(parents=True)
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("feed")
        sheet.append(["sku", "price", "note"])
        for sku in range(1, XLSX_ROWS + 1):
            price = 10 + sku % 300
            if side == "b" and sku % 1500 == 0:
                price += 1
            sheet.append([sku, f"{price}.00", f"note {sku}" if sku % 7 == 0 else None])
        workbook.save(target / "feed.xlsx")


def _build_zip(root: Path):
    folder = root / "zip"
    header = ["sku", "price"]
    for side in ("a", "b"):
        target = folder / side
        target.mkdir(parents=True)
        buffer = []
        for sku in range(1, ZIP_ROWS + 1):
            price = 10 + sku % 100
            if side == "b" and sku % 2000 == 0:
                price += 1
            buffer.append(f"{sku},{price}")
        payload = ",".join(header) + "\n" + "\n".join(buffer) + "\n"
        with zipfile.ZipFile(target / "feed.zip", "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("inner.csv", payload)


def _build_edge(root: Path):
    folder = root / "edge"
    a_dir, b_dir = folder / "a", folder / "b"
    a_dir.mkdir(parents=True)
    b_dir.mkdir(parents=True)

    # Header only, no data rows.
    _write_csv(a_dir / "empty.csv", ["sku", "price"], [])
    _write_csv(b_dir / "empty.csv", ["sku", "price"], [])

    # BOM + CRLF line endings.
    for side in ("a", "b"):
        target = a_dir if side == "a" else b_dir
        text = "sku,price\r\n1,10\r\n2,20\r\n"
        with open(target / "bom.csv", "w", encoding="utf-8-sig", newline="") as f:
            f.write(text)

    # Same columns in a different order (must still align by name).
    _write_csv(
        a_dir / "reordered.csv",
        ["sku", "title", "price"],
        [["1", "A", "10"], ["2", "B", "20"]],
    )
    _write_csv(
        b_dir / "reordered.csv",
        ["price", "sku", "title"],
        [["10", "1", "A"], ["20", "2", "B"]],
    )

    # Extra column on one side only.
    _write_csv(
        a_dir / "extra.csv",
        ["sku", "price", "only_a"],
        [["1", "10", "x"], ["2", "20", "y"]],
    )
    _write_csv(b_dir / "extra.csv", ["sku", "price"], [["1", "10"], ["2", "21"]])

    # SKU column entirely empty on one side -> nothing can align.
    _write_csv(a_dir / "nullsku.csv", ["sku", "price"], [["", "10"], ["", "20"]])
    _write_csv(b_dir / "nullsku.csv", ["sku", "price"], [["1", "10"], ["2", "20"]])

    # Unicode, emoji, combining marks, RTL and zero-width characters.
    header = ["sku", "title", "price"]
    a_rows = [
        ["1", "Café crème", "10.00"],
        ["2", "日本語の商品", "20.00"],
        ["3", "Emoji 🎉🎊 item", "30.00"],
        ["4", "e\u0301 combining", "40.00"],
        ["5", "עברית RTL", "50.00"],
        ["6", "zero\u200bwidth", "60.00"],
    ]
    b_rows = [
        ["1", "Café crème", "10.00"],
        ["2", "日本語の商品", "21.00"],  # price differs
        ["3", "Emoji 🎉🎊 item", "30.00"],
        ["4", "é combining", "40.00"],  # precomposed vs combining
        ["5", "עברית RTL", "50.00"],
        ["6", "zerowidth", "60.00"],  # zero-width space removed
    ]
    _write_csv(a_dir / "unicode.csv", header, a_rows)
    _write_csv(b_dir / "unicode.csv", header, b_rows)


_BUILDERS = {
    "big": _build_big,
    "wide": _build_wide,
    "many": _build_many,
    "xml": _build_xml,
    "xlsx": _build_xlsx,
    "zip": _build_zip,
    "edge": _build_edge,
}


def root() -> Path:
    """Return the shared temp root, building every dataset on first use."""
    global _ROOT
    if _ROOT is None:
        _ROOT = Path(tempfile.mkdtemp(prefix="feed_scale_"))
        atexit.register(shutil.rmtree, _ROOT, True)
        for name, builder in _BUILDERS.items():
            builder(_ROOT)
            print(f"  [scale data] built {name}")
    return _ROOT


def folder(name: str):
    """Return (a_dir, b_dir) for a dataset."""
    base = root() / name
    return base / "a", base / "b"


if __name__ == "__main__":
    target = root()
    print(f"Generated scale data in {target}")
