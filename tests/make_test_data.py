"""Deterministic test-feed generator.

Builds two parallel feed folders (``site_a`` / ``site_b``) whose differences
are known exactly, so the assertions in ``run_tests.py`` can check counts
rather than just "something happened".

Every pair of files exercises one specific code path:

  feed_main.csv      core diff logic, SKU normalization, missing/extra SKUs
  feed_numeric.csv   numeric / thousands / fullwidth / date / bool equivalence
  feed_gbk.csv       GBK-encoded source (must be transcoded, not mojibake)
  feed_semicolon.csv delimiter detection when a field contains a comma
  feed_pipe.csv      pipe-delimited source
  feed_xml.xml       namespaced (g:) XML feed
  feed_broken.xml    malformed XML that needs lxml recovery
  feed_excel.xlsx    Excel with sparse (null) cells
  feed_zip.zip       archive containing a CSV member
  feed_nosku.csv     no SKU-like column -> must be skipped, not guessed
  feed_dup.csv       duplicate SKUs -> first occurrence wins

Run directly to (re)generate ``tests/data``:

    python tests/make_test_data.py
"""
import csv
import io
import zipfile
from pathlib import Path

from openpyxl import Workbook


SITE_A = 'site_a'
SITE_B = 'site_b'

# --- feed_main.csv: the canonical expectations -----------------------------
# A has SKUs 1..38, B has 1..35 plus 39..40.
MAIN_SKUS_A = 38
MAIN_SKUS_B = 37
MAIN_COMMON = 35
MAIN_ONLY_A = 3
MAIN_ONLY_B = 2
# 5 price differences (31..35), 1 title difference (6), 1 one-sided stock (7)
MAIN_TOTAL_DIFFS = 7


def _write_csv(path: Path, header, rows, encoding='utf-8', delimiter=','):
    with open(path, 'w', encoding=encoding, newline='') as f:
        writer = csv.writer(f, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)


def _main_rows(side: str):
    """Rows for feed_main.csv. ``side`` is 'a' or 'b'."""
    if side == 'a':
        skus = range(1, MAIN_SKUS_A + 1)
    else:
        skus = list(range(1, 36)) + [39, 40]

    rows = []
    for i in skus:
        # A writes zero-padded SKUs ("00001"), B writes plain ones ("1").
        # Normalization must make them line up.
        sku = f"{i:05d}" if side == 'a' else str(i)
        title = f"Product {i}"
        if side == 'b' and i == 6:
            title = "product 6"          # deliberate title mismatch

        price = 10 + i
        if side == 'b' and 31 <= i <= 35:
            price += 1                   # deliberate price mismatch
        # A writes 2 decimals for the first 5 rows, B writes integers:
        # numerically equal, so these must still count as matches.
        if side == 'a' and i <= 5:
            price_text = f"{price:.2f}"
        else:
            price_text = str(price)

        stock = '100'
        if side == 'b' and i == 7:
            stock = ''                   # one-sided empty

        rows.append([sku, title, price_text, stock, 'USD', '2024-01-01'])
    return rows


def _numeric_rows(side: str):
    """Equivalence cases. Exactly one real mismatch (a5)."""
    if side == 'a':
        values = ['1,234.50', '１００', '2024-01-01', 'true', 'abc', '', '0.0']
    else:
        values = ['1234.5', '100', '2024/01/01', 'TRUE', 'ABC', '', '0']
    return [[f"a{idx + 1}", value] for idx, value in enumerate(values)]


def _xml_feed(prices, titles):
    items = []
    for idx, (price, title) in enumerate(zip(prices, titles), start=1):
        items.append(
            f"    <item>\n"
            f"      <g:id>{idx}</g:id>\n"
            f"      <title>{title}</title>\n"
            f"      <g:price>{price}</g:price>\n"
            f"    </item>"
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss xmlns:g="http://base.google.com/ns/1.0">\n'
        '  <channel>\n'
        + "\n".join(items) + "\n"
        '  </channel>\n'
        '</rss>\n'
    )


def _broken_xml(titles):
    # Unclosed root element and a bare ampersand: stdlib ElementTree gives up,
    # lxml's recover parser must still extract both items.
    items = "".join(
        f"<item><id>{idx}</id><title>{title}</title></item>"
        for idx, title in enumerate(titles, start=1)
    )
    return f'<rss><channel>{items}'


def _excel(path: Path, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(['sku', 'price', 'note'])
    for row in rows:
        sheet.append(row)
    workbook.save(path)


def _zip_with_csv(path: Path, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
        zf.writestr('inner.csv', buffer.getvalue())


def build(root: Path) -> Path:
    """Create site_a/site_b under ``root``. Returns ``root``."""
    root = Path(root)
    dir_a = root / SITE_A
    dir_b = root / SITE_B
    for d in (dir_a, dir_b):
        d.mkdir(parents=True, exist_ok=True)

    header = ['sku', 'title', 'price', 'stock', 'currency', 'updated_at']
    _write_csv(dir_a / 'feed_main.csv', header, _main_rows('a'))
    _write_csv(dir_b / 'feed_main.csv', header, _main_rows('b'))

    # --- equivalence cases -------------------------------------------------
    _write_csv(dir_a / 'feed_numeric.csv', ['sku', 'value'], _numeric_rows('a'))
    _write_csv(dir_b / 'feed_numeric.csv', ['sku', 'value'], _numeric_rows('b'))

    # --- GBK encoding ------------------------------------------------------
    gbk_a = [['1', '商品一', '10.00'], ['2', '商品二', '20.00']]
    gbk_b = [['1', '商品一', '10.00'], ['2', '商品二', '21.00']]
    _write_csv(dir_a / 'feed_gbk.csv', ['sku', '名称', 'price'], gbk_a, encoding='gbk')
    _write_csv(dir_b / 'feed_gbk.csv', ['sku', '名称', 'price'], gbk_b, encoding='gbk')

    # --- delimiter detection with an embedded comma ------------------------
    semi_a = [['1', 'Widget, large', '5'], ['2', 'Gadget', '6']]
    semi_b = [['1', 'Widget, large', '5'], ['2', 'Gadget', '7']]
    _write_csv(dir_a / 'feed_semicolon.csv', ['sku', 'name', 'qty'], semi_a, delimiter=';')
    _write_csv(dir_b / 'feed_semicolon.csv', ['sku', 'name', 'qty'], semi_b, delimiter=';')

    # --- pipe delimited ----------------------------------------------------
    pipe_a = [['1', 'widget', '5'], ['2', 'gadget', '6']]
    pipe_b = [['1', 'widget', '5'], ['2', 'gadget', '7']]
    _write_csv(dir_a / 'feed_pipe.csv', ['sku', 'name', 'qty'], pipe_a, delimiter='|')
    _write_csv(dir_b / 'feed_pipe.csv', ['sku', 'name', 'qty'], pipe_b, delimiter='|')

    # --- namespaced XML ----------------------------------------------------
    (dir_a / 'feed_xml.xml').write_text(
        _xml_feed(['10.00 USD', '20.00 USD', '30.00 USD'],
                  ['Product 1', 'Product 2', 'Product 3']),
        encoding='utf-8',
    )
    (dir_b / 'feed_xml.xml').write_text(
        _xml_feed(['10.00 USD', '21.00 USD', '30.00 USD'],
                  ['Product 1', 'Product 2', 'Product 3']),
        encoding='utf-8',
    )

    # --- malformed XML -----------------------------------------------------
    (dir_a / 'feed_broken.xml').write_text(
        _broken_xml(['A & B', 'C']), encoding='utf-8',
    )
    (dir_b / 'feed_broken.xml').write_text(
        _broken_xml(['A & B', 'D']), encoding='utf-8',
    )

    # --- Excel with sparse cells ------------------------------------------
    _excel(dir_a / 'feed_excel.xlsx', [
        ['1', '10.00', 'alpha'], ['2', '20.00', 'beta'],
        ['3', '30.00', None], ['4', '40.00', None], ['5', '50.00', None],
    ])
    _excel(dir_b / 'feed_excel.xlsx', [
        ['1', '10.00', 'alpha'], ['2', '20.00', 'beta'],
        ['3', '31.00', None], ['4', '40.00', None], ['5', '50.00', None],
    ])

    # --- ZIP containing a CSV ---------------------------------------------
    _zip_with_csv(dir_a / 'feed_zip.zip', ['sku', 'price'],
                  [['1', '10'], ['2', '20']])
    _zip_with_csv(dir_b / 'feed_zip.zip', ['sku', 'price'],
                  [['1', '10'], ['2', '25']])

    # --- no SKU-like column ------------------------------------------------
    _write_csv(dir_a / 'feed_nosku.csv', ['name', 'price'],
               [['Widget', '10'], ['Gadget', '20']])
    _write_csv(dir_b / 'feed_nosku.csv', ['name', 'price'],
               [['Widget', '10'], ['Gadget', '21']])

    # --- duplicate SKUs ----------------------------------------------------
    _write_csv(dir_a / 'feed_dup.csv', ['sku', 'price'],
               [['1', '10'], ['1', '999'], ['2', '20']])
    _write_csv(dir_b / 'feed_dup.csv', ['sku', 'price'],
               [['1', '10'], ['2', '20']])

    return root


if __name__ == '__main__':
    target = Path(__file__).resolve().parent / 'data'
    build(target)
    print(f"Generated test data in {target}")
