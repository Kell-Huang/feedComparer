# feedComparer

Desktop tool for comparing product feeds between two sources. It downloads
(or reads from disk) two versions of each feed, aligns them by SKU, and reports
cell-level differences per column.

Supports CSV, Excel (`.xlsx` / `.xls`), XML and ZIP archives containing any of
those.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

`fastexcel` is required for Excel support — `polars.read_excel` cannot open a
workbook without it.

## Run

```bash
python main.py
```

Three modes:

| Mode | Input |
|---|---|
| **File** | Two local folders; files with matching names are compared |
| **API** | Projects / sites / channels from the platform API, paired manually |
| **Manual URL** | Two pasted URL lists, paired by position |

API credentials live in `platform_config.json`:

```json
{
  "auth_token": "",
  "base_url": "https://platform-api.productsup.io/platform/v2",
  "verify_ssl": true,
  "download_timeout": 120
}
```

## How a run works

1. **Download** (API / Manual modes) — pairs are fetched concurrently, de-duplicated
   by URL, and the real format is detected from the payload rather than the URL
   suffix. Fills 0–60% of the progress bar.
2. **Validate** — each pair is read, aligned on a normalized SKU key, and every
   common column is diffed. Fills 60–100%.
3. **Report** — results render into six tabs and a ZIP report is written to
   `reports/` next to the application.

Every run can be cancelled from the toolbar; in-flight requests abort at the
next 256 KB chunk.

## Structure

```
main.py                 entry point: palette, stylesheet, exception hook
core/                   business logic, no Qt imports
  models.py             DownloadPair / DownloadResult / DownloadSession
  reader.py             CSV / Excel / XML / ZIP readers
  normalizer.py         SKU normalization (vectorized + reference impl)
  validator.py          pipeline: discover → read → align → diff → aggregate
  stats.py              column-level diff engine
  downloader.py         concurrent downloader with byte-level progress
  progress.py           ProgressUpdate / PairStatus / Throttle
  sku_detector.py       SKU column auto-detection
  logger.py             bounded, thread-safe debug log
ui/                     PySide6 widgets
  main_window.py        window, action bar, export
  results_panel.py      six result tabs
  file_mode_tab.py      file mode form
  api_mode_tab.py       API mode form
  manual_url_tab.py     manual URL form
  config_widget.py      shared column configuration
  pair_status.py        live per-pair status column
  delegates.py          status pill delegate
  theme.py              design tokens + stylesheet loader
  icons.py              inline SVG icons
  dialogs.py            column picker, completion dialog
  workers.py            QThread workers
  layout_utils.py       scrollable tab container
styles/main.qss         stylesheet
assets/                 SVG assets referenced by the stylesheet
tests/                  test suites + fixture generator
```

## Tests

```bash
python tests/run_tests.py           # everything (~20s)
python tests/run_tests.py core      # one suite
python tests/run_tests.py scale     # large-batch + edge cases
```

Suites: `core`, `progress`, `downloader`, `ui`, `flow`, `scale`. No pytest
required, though every test is pytest-compatible.

The `scale` suite generates its fixtures into a temporary directory at runtime
(150k rows × 20 columns, 40k × 119 columns, 40 pairs, 10k XML items, 15k Excel
rows, 80k rows inside a ZIP) and deletes them on exit. Diff counts are asserted
exactly, not just "something was found".

## Notes

- CSV and XML are read as **strings** on purpose. Letting Polars infer types
  turns a value like `N/A` in a mostly-numeric column into `null`, which hides
  a real difference.
- Feed comparison is case-sensitive for values; the "Case Sensitive" toggle
  applies to SKU matching.
- Excluded column keywords default to `stock, price` and are editable per run
  in the configuration panel.
