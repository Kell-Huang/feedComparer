# feedComparer

Desktop tool for comparing product feeds between two sources. It downloads
(or reads from disk) two versions of each feed, aligns them by SKU, and reports
cell-level differences per column. CSV, Excel (`.xlsx` / `.xls`), XML and ZIP
archives of those are supported.

## Install

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

`fastexcel` is required for Excel — `polars.read_excel` cannot open a workbook
without it.

## Run

```bash
python main.py
```

| Mode | Input |
|---|---|
| **File** | Two local folders; files with matching names are compared |
| **API** | Projects / sites / channels from the platform API, paired manually |
| **Manual URL** | Two pasted URL lists, paired by position |

SKU column, compare columns, exclusions and flags live on the **Settings** tab:
one configuration shared by all three modes, recapped in the action bar. API
credentials live in `platform_config.json` (`auth_token`, `base_url`,
`verify_ssl`, `download_timeout`).

## How a run works

Download (API / Manual) → validate → report. Pairs are fetched concurrently and
de-duplicated by URL, the format is detected from the payload rather than the
URL suffix, and each pair is aligned on a normalized SKU key before every
common column is diffed. Results render into six tabs and a ZIP report is
written to `reports/`. Runs are cancellable; in-flight requests abort at the
next 256 KB chunk.

## Structure

```
main.py              entry point: palette, stylesheet, exception hook
core/                business logic, no Qt imports
  reader.py          CSV / Excel / XML / ZIP readers
  validator.py       discover → read → align → diff → aggregate
  stats.py           column-level diff engine
  downloader.py      concurrent downloader with byte-level progress
  normalizer.py      SKU normalization
  sku_detector.py    SKU column auto-detection
  models.py progress.py logger.py
ui/                  PySide6 widgets
  main_window.py     window, tabs, action bar, export
  results_panel.py   six result tabs
  file_mode_tab.py api_mode_tab.py manual_url_tab.py
  config_widget.py   column configuration (Settings tab)
  pairing_canvas.py  drag-to-pair map for manual mode
  workers.py         QThread workers
  dialogs.py icons.py theme.py delegates.py pair_status.py layout_utils.py
styles/main.qss      stylesheet
assets/              app icon (svg / ico / png) + stylesheet SVGs
tools/make_icon.py   rebuilds the .ico from assets/app_icon.svg
tests/               test suites + fixture generator
```

## Tests

```bash
python tests/run_tests.py           # everything (~20s)
python tests/run_tests.py scale     # one suite: core, progress, downloader,
                                    #            ui, flow, scale
```

The `scale` suite builds its fixtures at runtime (150k × 20, 40k × 119, 40
pairs, 10k XML items, 15k Excel rows, 80k rows in a ZIP) and deletes them on
exit. Diff counts are asserted exactly, not just "something was found".

## Formatting

`isort . && black .` — both read `pyproject.toml`, so pass no flags and set none
in your editor.

## Notes

- CSV and XML are read as **strings** on purpose: letting Polars infer types
  turns `N/A` in a mostly-numeric column into `null`, which hides a real
  difference.
- Values are compared case-sensitively; the "Case Sensitive" toggle applies to
  SKU matching.
- Excluded column keywords default to `stock, price`, editable on the Settings
  tab; changes apply to the next run, not to one in flight.
