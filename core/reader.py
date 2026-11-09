"""Multi-format file readers for CSV / Excel / XML / ZIP inputs.

The DebugLogger instance is shared with the caller. All read methods take an
explicit ``filename`` argument so log output stays correct under concurrent
access; relying on a shared ``logger.current_file`` races across threads.

Type policy
-----------
CSV and XML sources are read as **strings** (``infer_schema_length=0``).
A feed comparison tool must compare what the feed actually says, so letting
Polars infer numeric/datetime dtypes is counter-productive: a column whose
first 100 rows are integers but whose row 5000 is ``"N/A"`` would silently
turn that value into ``null`` under ``ignore_errors=True``, hiding a real
difference. Strings also make the cross-format comparisons in
``ColumnMatchStats`` (numeric / date / bool equivalence) work uniformly.

Excel keeps native dtypes because the workbook itself carries them and both
sides of a pair are read the same way.

Encoding policy
---------------
Polars' CSV reader only accepts UTF-8 (``utf8`` / ``utf8-lossy``), so the
previous encoding-retry loop could never read a GBK file. We sniff the
encoding from a 1 MB prefix with an incremental decoder (which tolerates a
multi-byte character split across the sample boundary), pass UTF-8 files
straight to Polars so it can stream them, and stream-transcode anything else
to a temporary UTF-8 file so memory stays bounded for large inputs.
"""
import codecs
import csv
import os
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import polars as pl

from .logger import DebugLogger


SUPPORTED_EXTENSIONS = {'.csv', '.xlsx', '.xls', '.xml', '.zip'}
ARCHIVE_EXTENSIONS = {'.csv', '.xlsx', '.xls', '.xml'}

# Encoding candidates, in preference order. latin-1 is the last resort because
# it can decode any byte sequence and therefore never fails.
_ENCODINGS = ('utf-8-sig', 'utf-8', 'gb18030', 'big5', 'cp1252', 'latin-1')
_UTF8_ENCODINGS = {'utf-8', 'utf-8-sig'}

_SNIFF_SAMPLE_SIZE = 1 << 20          # 1 MB
_TRANSCODE_CHUNK = 1 << 20            # 1 MB

_DELIMITERS = '|;\t,~^'
_DELIMITER_CANDIDATES = ('|', '\t', ';', ',', '~', '^')

# Single pass: strip the XML declaration (only at the very start) and every
# character outside the XML 1.0 legal set. One regex pass = one string copy
# instead of the previous four.
_XML_CLEAN_RE = re.compile(
    r'\A\s*<\?xml[^>]*\?>'
    r'|[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]',
    re.IGNORECASE,
)

_REPEATING_TAGS = ('item', 'product', 'offer', 'record', 'row', 'entry')


def _clean_tag(tag: str) -> str:
    """Strip ``{namespace}`` URIs and short ``g:`` style prefixes from a tag."""
    if not isinstance(tag, str):
        return str(tag)
    if '}' in tag:
        tag = tag.split('}', 1)[1]
    if ':' in tag:
        head, _, tail = tag.partition(':')
        if len(head) <= 3:
            tag = tail
    return tag


def _fill_empty_strings(df: pl.DataFrame) -> pl.DataFrame:
    """Replace nulls with "" for string columns only.

    ``DataFrame.fill_null("")`` raises for numeric/datetime columns, so the
    previous blanket call could not read an Excel file with a sparse numeric
    column at all.
    """
    str_cols = [name for name, dtype in df.schema.items() if dtype == pl.String]
    if str_cols:
        df = df.with_columns([pl.col(c).fill_null('') for c in str_cols])
    return df


def _element_records(root) -> list:
    """Flatten repeating child elements under ``root`` into record dicts.

    Nested elements are flattened as ``parent_child``. Attributes are kept
    with an ``@`` prefix. Namespace URIs and prefixes are stripped so that
    ``g:id`` and ``{http://...}id`` both become ``id``.
    """
    items = []
    for tag in _REPEATING_TAGS:
        items = root.findall(f'.//{tag}')
        if not items:
            items = root.findall(f'.//{{*}}{tag}')
        if items:
            break
    if not items:
        items = list(root)

    records = []
    for item in items:
        record = {}
        for child in item:
            child_tag = _clean_tag(child.tag)
            grandchildren = list(child)
            if grandchildren:
                for subchild in grandchildren:
                    key = f"{child_tag}_{_clean_tag(subchild.tag)}"
                    record[key] = (subchild.text or '').strip()
            else:
                record[child_tag] = (child.text or '').strip()
        for key, val in item.attrib.items():
            record[f"@{_clean_tag(key)}"] = val
        records.append(record)
    return records


class FileReader:
    """Read CSV, Excel, XML and ZIP-embedded inputs into Polars DataFrames."""

    def __init__(self, logger: DebugLogger):
        self.logger = logger

    # ------------------------------------------------------------------
    # Encoding
    # ------------------------------------------------------------------

    @staticmethod
    def _sniff_encoding(sample: bytes) -> str:
        """Detect the most likely encoding for a byte sample.

        Uses an incremental decoder with ``final=False`` so a multi-byte
        character split across the sample boundary does not produce a false
        negative.
        """
        for enc in _ENCODINGS:
            try:
                decoder = codecs.getincrementaldecoder(enc)()
                decoder.decode(sample, final=False)
                return enc
            except (UnicodeDecodeError, LookupError):
                continue
        return 'latin-1'

    def _detect_encoding(self, file_path: Path) -> str:
        try:
            with open(file_path, 'rb') as f:
                sample = f.read(_SNIFF_SAMPLE_SIZE)
        except OSError:
            return 'utf-8'
        return self._sniff_encoding(sample)

    def _transcode_to_temp(self, file_path: Path, encoding: str) -> Path:
        """Stream-decode ``file_path`` into a temporary UTF-8 file."""
        fd, tmp_name = tempfile.mkstemp(suffix='.csv', prefix='feed_transcoded_')
        tmp_path = Path(tmp_name)
        try:
            with open(file_path, 'rb') as src, \
                    os.fdopen(fd, 'w', encoding='utf-8', newline='') as dst:
                decoder = codecs.getincrementaldecoder(encoding)(errors='replace')
                while True:
                    chunk = src.read(_TRANSCODE_CHUNK)
                    if not chunk:
                        break
                    dst.write(decoder.decode(chunk))
                dst.write(decoder.decode(b'', final=True))
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise
        return tmp_path

    # ------------------------------------------------------------------
    # Delimiters
    # ------------------------------------------------------------------

    @staticmethod
    def _sniff_delimiter(sample_text: str) -> str:
        try:
            return csv.Sniffer().sniff(sample_text, delimiters=_DELIMITERS).delimiter
        except Exception:
            return ','

    def _detect_separator(self, file_path: Path) -> str:
        """Auto-detect the CSV delimiter of a file on disk."""
        encoding = self._detect_encoding(file_path)
        try:
            with open(file_path, 'r', encoding=encoding, errors='replace') as f:
                sample = f.read(8192)
        except OSError:
            return ','
        return self._sniff_delimiter(sample)

    def _detect_separator_from_bytes(self, data: bytes) -> str:
        """Auto-detect the CSV delimiter from an in-memory payload."""
        text = data.decode(self._sniff_encoding(data), errors='replace')
        return self._sniff_delimiter(text[:8192])

    # ------------------------------------------------------------------
    # CSV
    # ------------------------------------------------------------------

    @staticmethod
    def _csv_options(separator: str) -> dict:
        return {
            # Read every column as a string: see the module docstring.
            'infer_schema_length': 0,
            'separator': separator,
            'has_header': True,
            'null_values': [],
            'truncate_ragged_lines': True,
            'encoding': 'utf8',
        }

    def _read_csv_bytes(self, raw: bytes, separator: str) -> pl.DataFrame:
        """Parse an in-memory CSV payload. Non-UTF-8 payloads are transcoded."""
        encoding = self._sniff_encoding(raw)
        if encoding not in _UTF8_ENCODINGS:
            raw = raw.decode(encoding, errors='replace').encode('utf-8')
        elif encoding == 'utf-8-sig':
            raw = raw.decode('utf-8-sig').encode('utf-8')
        return pl.read_csv(raw, **self._csv_options(separator))

    def _read_csv_path(self, file_path: Path, separator: str,
                       n_rows: int = None) -> pl.DataFrame:
        """Parse a CSV on disk, letting Polars stream it when it is UTF-8."""
        encoding = self._detect_encoding(file_path)
        options = self._csv_options(separator)
        if n_rows is not None:
            options['n_rows'] = n_rows
        if encoding in _UTF8_ENCODINGS:
            return pl.read_csv(str(file_path), **options)

        self.logger.debug(
            f"  Non-UTF-8 source detected ({encoding}); transcoding to UTF-8",
            filename=file_path.name,
        )
        tmp_path = self._transcode_to_temp(file_path, encoding)
        try:
            return pl.read_csv(str(tmp_path), **options)
        finally:
            tmp_path.unlink(missing_ok=True)

    def _read_csv_with_retry(self, file_path: Path, filename: str,
                             is_zip: bool = False, zip_file=None,
                             csv_name: str = None) -> pl.DataFrame:
        if is_zip:
            with zip_file.open(csv_name) as f:
                raw_bytes = f.read()
            sep = self._detect_separator_from_bytes(raw_bytes)
            read = lambda delimiter: self._read_csv_bytes(raw_bytes, delimiter)
        else:
            sep = self._detect_separator(file_path)
            read = lambda delimiter: self._read_csv_path(file_path, delimiter)

        try:
            df = read(sep)
        except Exception as e:
            source = f"{file_path}::{csv_name}" if is_zip else str(file_path)
            raise ValueError(f"Cannot read CSV: {source} - {e}") from e

        if df.width == 1:
            # A single column usually means the sniffer guessed wrong (common
            # with single-column or heavily-quoted files).
            self.logger.debug(
                "  Only 1 column detected, retrying other delimiters...",
                filename=filename,
            )
            best_df = df
            for delimiter in _DELIMITER_CANDIDATES:
                if delimiter == sep:
                    continue
                try:
                    candidate = read(delimiter)
                except Exception:
                    continue
                if candidate.width > best_df.width:
                    best_df = candidate
                if best_df.width >= 3:
                    break
            df = best_df
        return df

    # ------------------------------------------------------------------
    # XML
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_xml_root(text: str, filename: str, logger: DebugLogger):
        """Parse XML text, degrading from strict to lenient parsers."""
        try:
            return ET.fromstring(text), 'ElementTree'
        except ET.ParseError:
            pass

        try:
            import lxml.etree as lxml_etree
        except ImportError:
            raise ValueError(
                "Malformed XML and lxml is not installed, so recovery parsing "
                "is unavailable. Install lxml (see requirements.txt)."
            ) from None

        try:
            parser = lxml_etree.XMLParser(recover=True, huge_tree=True)
            return lxml_etree.fromstring(text, parser=parser), 'lxml (recover)'
        except Exception:
            pass

        try:
            parser = lxml_etree.HTMLParser(recover=True)
            root = lxml_etree.fromstring(text, parser=parser)
            if root is not None and root.tag == 'html':
                body = root.find('body')
                if body is not None and len(body) > 0:
                    root = body
            return root, 'lxml (HTML fallback)'
        except Exception as e:
            raise ValueError(f"Could not parse XML: {e}") from e

    def _xml_dataframe(self, raw_bytes: bytes, filename: str) -> pl.DataFrame:
        if raw_bytes[:3] == b'\xef\xbb\xbf':
            raw_bytes = raw_bytes[3:]

        text = raw_bytes.decode('utf-8', errors='replace')
        text = _XML_CLEAN_RE.sub('', text)

        root, parser_name = self._parse_xml_root(text, filename, self.logger)
        self.logger.debug(f"  Parsed with {parser_name}", filename=filename)

        if root is None:
            raise ValueError("Could not find repeating data elements in XML")

        records = _element_records(root)
        if not records:
            raise ValueError("No data extracted from XML")

        df = pl.from_dicts(records, infer_schema_length=None)
        df = _fill_empty_strings(df).select(pl.all().cast(pl.String, strict=False))
        self.logger.debug(
            f"  XML converted: {df.height:,} rows, {df.width} cols",
            filename=filename,
        )
        return df

    def _read_xml_file(self, file_path: Path, filename: str) -> pl.DataFrame:
        self.logger.debug("  Parsing XML file...", filename=filename)
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()
        return self._xml_dataframe(raw_bytes, filename)

    # ------------------------------------------------------------------
    # ZIP
    # ------------------------------------------------------------------

    def _read_zip_file(self, file_path: Path, filename: str) -> pl.DataFrame:
        """Read the first supported file from a ZIP archive."""
        self.logger.debug(f"  Opening ZIP file: {file_path.name}", filename=filename)
        with zipfile.ZipFile(file_path, 'r') as zf:
            for name in zf.namelist():
                if name.endswith('/'):
                    continue
                ext = Path(name).suffix.lower()
                if ext not in ARCHIVE_EXTENSIONS:
                    continue
                self.logger.debug(f"  Reading '{name}' from ZIP", filename=filename)
                if ext == '.csv':
                    return self._read_csv_with_retry(
                        file_path, filename, is_zip=True, zip_file=zf, csv_name=name,
                    )
                if ext in {'.xlsx', '.xls'}:
                    with zf.open(name) as f:
                        return self._read_excel_bytes(f.read())
                # .xml: reuse the same extraction path as a plain XML file so
                # both produce identical columns.
                with zf.open(name) as f:
                    return self._xml_dataframe(f.read(), filename)
        raise ValueError(
            f"No supported file (CSV/Excel/XML) found inside ZIP: {file_path.name}"
        )

    # ------------------------------------------------------------------
    # Excel
    # ------------------------------------------------------------------

    @staticmethod
    def _excel_options() -> dict:
        return {
            # Same policy as CSV: keep the raw textual representation so a
            # mixed column cannot have values coerced to null by inference.
            'infer_schema_length': 0,
            'has_header': True,
        }

    def _read_excel_path(self, path: Path) -> pl.DataFrame:
        try:
            df = pl.read_excel(str(path), **self._excel_options())
        except Exception as e:
            raise ValueError(
                f"Cannot read Excel file {path.name}: {e}. Excel support "
                "requires the 'fastexcel' package (see requirements.txt)."
            ) from e
        return _fill_empty_strings(df)

    def _read_excel_bytes(self, raw: bytes) -> pl.DataFrame:
        try:
            df = pl.read_excel(raw, **self._excel_options())
        except Exception as e:
            raise ValueError(f"Cannot read Excel member inside ZIP: {e}") from e
        return _fill_empty_strings(df)

    # ------------------------------------------------------------------
    # Column discovery
    # ------------------------------------------------------------------

    def read_columns(self, file_path) -> list:
        """Return the column names of a file without reading its rows.

        CSV and Excel only touch the header row, so this stays fast even for
        very large inputs. XML / ZIP require a structural parse because their
        column names cannot be inferred from a prefix.

        Excel note: Polars has no header-only mode (``n_rows`` is not a
        ``read_excel`` parameter), so modern workbooks are opened with
        openpyxl in read-only mode, which streams instead of loading the
        sheet. Legacy ``.xls`` (OLE2) is not supported by openpyxl and falls
        back to a full read.
        """
        path = Path(file_path)
        suffix = path.suffix.lower()
        if suffix == '.csv':
            separator = self._detect_separator(path)
            return list(self._read_csv_path(path, separator, n_rows=0).columns)
        if suffix in {'.xlsx', '.xlsm'}:
            header = self._xlsx_header(path)
            if header:
                return header
        if suffix in {'.xlsx', '.xls', '.xlsm'}:
            return list(self._read_excel_path(path).columns)
        return list(self._read_file(path, filename=path.name).columns)

    @staticmethod
    def _xlsx_header(path: Path) -> list:
        """Read just the first row of an xlsx workbook. Empty list on failure."""
        try:
            from openpyxl import load_workbook
        except ImportError:
            return []
        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
        except Exception:
            return []
        try:
            sheet = workbook.worksheets[0]
            for row in sheet.iter_rows(min_row=1, max_row=1, values_only=True):
                return [
                    str(cell) if cell is not None else f"column_{idx + 1}"
                    for idx, cell in enumerate(row)
                ]
            return []
        finally:
            workbook.close()

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _read_file(self, file_path: Path, filename: str) -> pl.DataFrame:
        suffix = file_path.suffix.lower()
        if suffix == '.xml':
            return self._read_xml_file(file_path, filename)
        if suffix == '.zip':
            return self._read_zip_file(file_path, filename)
        if suffix == '.csv':
            df = self._read_csv_with_retry(file_path, filename)
        elif suffix in {'.xlsx', '.xls', '.xlsm'}:
            df = self._read_excel_path(file_path)
        else:
            raise ValueError(f"Unsupported format: {suffix}")

        self.logger.debug(
            f"  Read OK: rows={df.height:,}, cols={df.width}",
            filename=filename,
        )
        return df
