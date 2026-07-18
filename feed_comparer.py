import sys
import json
import os
import csv
import gc
import zipfile
import shutil
import tempfile
from io import BytesIO, StringIO
from pathlib import Path
from datetime import datetime
from typing import List, Tuple, Optional, Dict, Any, Set
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import re
import lxml
import unicodedata
import traceback
from collections import defaultdict
import xml.etree.ElementTree as ET
import pandas as pd
import polars as pl
import numpy as np
import requests

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QLineEdit, QFileDialog, QTextEdit,
    QProgressBar, QTableWidget, QTableWidgetItem, QTabWidget,
    QCheckBox, QComboBox, QGroupBox, QGridLayout, QSplitter,
    QHeaderView, QMessageBox, QStyle, QSpinBox, QPlainTextEdit,
    QDialog, QListWidget, QListWidgetItem, QAbstractItemView, QFrame,
    QSizePolicy
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QColor, QPalette


# ============================================================================
# Intelligent SKU Column Detector (with Priority List & Edit Distance)
# ============================================================================

class SKUColumnDetector:
    # Priority order for target keywords (ean > at_sku > sku > id)
    TARGET_KEYWORDS = ['ean', 'at_sku', 'sku', 'id']

    SKU_PATTERNS = [
        {'pattern': r'^sku$', 'weight': 1.0, 'label': 'SKU'},
        {'pattern': r'^id$', 'weight': 0.95, 'label': 'ID'},
        {'pattern': r'^ean$', 'weight': 0.9, 'label': 'EAN'},
        {'pattern': r'^gtin$', 'weight': 0.9, 'label': 'GTIN'},
        {'pattern': r'^upc$', 'weight': 0.9, 'label': 'UPC'},
        {'pattern': r'^mpn$', 'weight': 0.9, 'label': 'MPN'},
        {'pattern': r'^offerid$', 'weight': 0.95, 'label': 'OfferID'},
        {'pattern': r'^productid$', 'weight': 0.95, 'label': 'ProductID'},
        {'pattern': r'^itemid$', 'weight': 0.9, 'label': 'ItemID'},
        {'pattern': r'^articleid$', 'weight': 0.9, 'label': 'ArticleID'},
        {'pattern': r'^sku[_\s]?(id|code|num|no|number)?$', 'weight': 0.95, 'label': 'SKU variant'},
        {'pattern': r'^(product|item|article|offer)[_\s]?(sku|id|code)$', 'weight': 0.9, 'label': 'Product/Item ID'},
        {'pattern': r'^(product|item|article|offer)[_\s]?(number|num|no)$', 'weight': 0.85, 'label': 'Product Number'},
        {'pattern': r'^.*(sku|stock[_\s]?keeping[_\s]?unit).*$', 'weight': 0.8, 'label': 'Contains SKU'},
        {'pattern': r'^ean[_\s]?(code|number|num|no)?$', 'weight': 0.9, 'label': 'EAN variant'},
        {'pattern': r'^gtin[_\s]?(code|number|num|no)?$', 'weight': 0.9, 'label': 'GTIN variant'},
        {'pattern': r'^upc[_\s]?(code|number|num|no)?$', 'weight': 0.9, 'label': 'UPC variant'},
        {'pattern': r'^mpn[_\s]?(code|number|num|no)?$', 'weight': 0.85, 'label': 'MPN variant'},
        {'pattern': r'^model[_\s]?(number|num|no|id|code)?$', 'weight': 0.7, 'label': 'Model Number'},
        {'pattern': r'^part[_\s]?(number|num|no|id|code)?$', 'weight': 0.7, 'label': 'Part Number'},
    ]

    EXCLUDE_PATTERNS = [
        r'^category[_\s]?id$', r'^parent[_\s]?id$', r'^group[_\s]?id$',
        r'^manufacturer[_\s]?id$', r'^brand[_\s]?id$', r'^country[_\s]?id$',
        r'^tax[_\s]?id$', r'^currency[_\s]?id$', r'^language[_\s]?id$',
        r'^warehouse[_\s]?id$', r'^store[_\s]?id$', r'^shop[_\s]?id$',
        r'^row[_\s]?id$', r'^index$', r'^unnamed',
    ]

    def __init__(self, user_specified: Optional[str] = None, priority_list: Optional[List[str]] = None):
        self.user_specified = user_specified
        self.priority_list = priority_list or []
        self.detection_log = []

    def normalize_col(self, col_name: str) -> str:
        n = str(col_name).strip().lower()
        n = n.strip('"').strip("'")
        # Handle namespace prefixes: g:id -> id, c:price -> price
        # Keep the part after the last colon if it looks like a namespace prefix
        if ':' in n:
            parts = n.split(':')
            # If the part before colon is short (likely namespace), use only the part after
            if len(parts[0]) <= 3:
                n = parts[-1]
        n = re.sub(r'[\s\-_]+', '_', n)
        n = re.sub(r'[^a-z0-9_]', '', n)
        n = n.strip('_')
        return n

    def is_excluded(self, col_name: str) -> bool:
        normalized = self.normalize_col(col_name)
        for pattern in self.EXCLUDE_PATTERNS:
            if re.match(pattern, normalized, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _levenshtein_distance(s1: str, s2: str) -> int:
        """Calculate Levenshtein distance between two strings."""
        if len(s1) < len(s2):
            return SKUColumnDetector._levenshtein_distance(s2, s1)
        if len(s2) == 0:
            return len(s1)
        prev_row = range(len(s2) + 1)
        for i, c1 in enumerate(s1):
            curr_row = [i + 1]
            for j, c2 in enumerate(s2):
                cost = 0 if c1 == c2 else 1
                curr_row.append(min(curr_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost))
            prev_row = curr_row
        return prev_row[-1]

    def score_column(self, col_name: str) -> Tuple[float, str]:
        """Score a column name using priority list, patterns, and edit distance."""
        # 1. User specified exact match
        if self.user_specified:
            if col_name == self.user_specified:
                return 1.0, 'User specified (exact)'
            if col_name.lower() == self.user_specified.lower():
                return 0.98, 'User specified (case-insensitive)'
            if self.normalize_col(col_name) == self.normalize_col(self.user_specified):
                return 0.96, 'User specified (normalized)'

        # 2. Priority list
        if self.priority_list:
            for idx, priority_col in enumerate(self.priority_list):
                priority_col = priority_col.strip()
                if not priority_col:
                    continue
                if col_name == priority_col:
                    return max(0.95 - (idx * 0.02), 0.8), f'Priority #{idx + 1} (exact)'
                if col_name.lower() == priority_col.lower():
                    return max(0.93 - (idx * 0.02), 0.78), f'Priority #{idx + 1} (case-insensitive)'
                if self.normalize_col(col_name) == self.normalize_col(priority_col):
                    return max(0.91 - (idx * 0.02), 0.76), f'Priority #{idx + 1} (normalized)'

        # 3. Exclude patterns
        if self.is_excluded(col_name):
            return 0.0, 'Excluded'

        # 4. Exact pattern match (high weight)
        normalized = self.normalize_col(col_name)
        for pattern_def in self.SKU_PATTERNS:
            if re.match(pattern_def['pattern'], normalized, re.IGNORECASE):
                return pattern_def['weight'], pattern_def['label']

        # 5. Edit distance based scoring with priority keywords
        normalized_lower = normalized.lower()
        best_score = 0.0
        best_keyword = ''
        for kw in self.TARGET_KEYWORDS:
            # Calculate similarity based on edit distance
            dist = self._levenshtein_distance(normalized_lower, kw)
            max_len = max(len(normalized_lower), len(kw))
            if max_len == 0:
                continue
            similarity = 1 - (dist / max_len)
            # Penalize long column names
            length_penalty = min(len(normalized_lower) / 20, 0.3)
            score = (similarity - length_penalty) * 0.7
            score = max(0, min(0.7, score))
            # Priority bonus: the earlier in TARGET_KEYWORDS, the higher the bonus
            priority_bonus = (len(self.TARGET_KEYWORDS) - self.TARGET_KEYWORDS.index(kw)) * 0.05
            score += priority_bonus
            if score > best_score:
                best_score = score
                best_keyword = kw

        # 6. Contains keyword fallback
        if best_score < 0.4:
            keywords = ['sku', 'ean', 'gtin', 'upc', 'mpn', 'offerid', 'productid', 'itemid']
            for kw in keywords:
                if kw in normalized_lower:
                    if normalized_lower == kw:
                        return 0.65, f'Exact match "{kw}"'
                    bonus = max(0, (15 - len(normalized_lower)) * 0.003)
                    return min(0.55 + bonus, 0.65), f'Contains "{kw}"'
            return 0.0, 'No match'

        if best_score >= 0.4:
            return best_score, f'Similar to "{best_keyword}" (distance)'
        return 0.0, 'No match'

    def detect_sku_column(self, columns: List[str], file_name: str = "") -> Tuple[Optional[str], float, str, Dict]:
        candidates = {}
        for col in columns:
            score, label = self.score_column(col)
            if score > 0:
                candidates[col] = (score, label)
        if not candidates:
            self.detection_log.append(f"[{file_name}] ❌ No SKU column detected. Available: {columns[:10]}...")
            return None, 0.0, 'Not found', {}
        sorted_candidates = sorted(candidates.items(), key=lambda x: (-x[1][0], len(x[0])))
        best_col, (best_score, best_label) = sorted_candidates[0]
        candidate_str = ', '.join([f"'{c}'({s:.2f}:{l})" for c, (s, l) in sorted_candidates[:5]])
        self.detection_log.append(f"[{file_name}] 🔍 SKU candidates: {candidate_str}")
        if best_score >= 0.8:
            self.detection_log.append(f"[{file_name}] ✅ Detected: '{best_col}' (score: {best_score:.2f}, {best_label})")
        elif best_score >= 0.5:
            self.detection_log.append(f"[{file_name}] ⚠️ Low confidence: '{best_col}' (score: {best_score:.2f}, {best_label})")
        else:
            self.detection_log.append(f"[{file_name}] ❌ No reliable SKU column found")
        return best_col, best_score, best_label, dict(sorted_candidates)

    def detect_common_sku(self, df1_columns: List[str], df2_columns: List[str],
                          file_name: str = "") -> Tuple[Optional[str], Optional[str], float]:
        col1, score1, label1, cands1 = self.detect_sku_column(df1_columns, f"{file_name} (File1)")
        col2, score2, label2, cands2 = self.detect_sku_column(df2_columns, f"{file_name} (File2)")
        if col1 is None or col2 is None:
            return None, None, 0.0
        confidence = min(score1, score2)
        if col1 != col2:
            self.detection_log.append(f"[{file_name}] ⚠️ Different SKU columns: File1='{col1}', File2='{col2}'")
        return col1, col2, confidence

    def get_log(self) -> List[str]:
        return self.detection_log

    def clear_log(self):
        self.detection_log.clear()


# ============================================================================
# Column Selector Dialog
# ============================================================================

class ColumnSelectorDialog(QDialog):
    def __init__(self, parent, file_path: str, currently_selected: List[str] = None):
        super().__init__(parent)
        self.setWindowTitle('Select Columns to Compare')
        self.setMinimumSize(500, 550)
        self.selected_columns = []
        self.currently_selected = currently_selected or []

        self.setStyleSheet("""
            QDialog { background-color: white; }
            QLabel { color: #1a1a2e; font-size: 13px; }
            QLabel#titleLabel { font-size: 16px; font-weight: bold; color: #1a1a2e; }
            QLabel#fileLabel { color: #595959; font-size: 12px; padding: 5px; background-color: #f5f6fa; border-radius: 4px; }
            QListWidget { border: 1px solid #d9d9d9; border-radius: 4px; background: white; color: #1a1a2e; font-size: 13px; }
            QListWidget::item { padding: 8px; border-bottom: 1px solid #f0f0f0; }
            QCheckBox { color: #1a1a2e; font-size: 13px; spacing: 8px; }
            QCheckBox::indicator { width: 18px; height: 18px; }
            QPushButton { background-color: #4096ff; color: white; border: none; padding: 10px 24px; border-radius: 6px; font-weight: bold; font-size: 13px; }
            QPushButton:hover { background-color: #1677ff; }
            QPushButton#cancelBtn { background-color: #f0f0f0; color: #1a1a2e; }
            QPushButton#selectAllBtn, QPushButton#deselectAllBtn { background-color: #f0f0f0; font-weight: normal; padding: 6px 12px; font-size: 12px; }
            QPushButton#selectAllBtn { color: #4096ff; }
            QPushButton#deselectAllBtn { color: #595959; }
            QLineEdit { padding: 8px; border: 1px solid #d9d9d9; border-radius: 4px; background: white; color: #1a1a2e; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_label = QLabel("📋 Select Columns to Compare")
        title_label.setObjectName("titleLabel")
        layout.addWidget(title_label)

        file_label = QLabel(f"📄 Sample file: {Path(file_path).name}")
        file_label.setObjectName("fileLabel")
        file_label.setWordWrap(True)
        layout.addWidget(file_label)

        desc_label = QLabel("Check the columns you want to compare. Unchecked columns will be ignored.")
        desc_label.setStyleSheet('color: #595959; font-size: 12px;')
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        search_layout = QHBoxLayout()
        search_layout.addWidget(QLabel("🔍 Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Filter columns...')
        self.search_edit.textChanged.connect(self._filter_columns)
        search_layout.addWidget(self.search_edit)
        layout.addLayout(search_layout)

        select_btns = QHBoxLayout()
        select_all_btn = QPushButton('Select All')
        select_all_btn.setObjectName('selectAllBtn')
        select_all_btn.clicked.connect(lambda: self._toggle_all(True))
        select_btns.addWidget(select_all_btn)
        deselect_all_btn = QPushButton('Deselect All')
        deselect_all_btn.setObjectName('deselectAllBtn')
        deselect_all_btn.clicked.connect(lambda: self._toggle_all(False))
        select_btns.addWidget(deselect_all_btn)
        select_btns.addStretch()
        layout.addLayout(select_btns)

        self.column_list = QListWidget()
        self.column_list.setSelectionMode(QAbstractItemView.NoSelection)
        layout.addWidget(self.column_list, stretch=1)

        self.stats_label = QLabel()
        self.stats_label.setStyleSheet('color: #595959; font-size: 12px;')
        layout.addWidget(self.stats_label)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton('Cancel')
        cancel_btn.setObjectName('cancelBtn')
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        ok_btn = QPushButton('✓ Apply Selection')
        ok_btn.clicked.connect(self._apply_selection)
        btn_layout.addWidget(ok_btn)
        layout.addLayout(btn_layout)

        self._load_columns(file_path)

    def _detect_sep(self, file_path: str) -> str:
        try:
            for enc in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                try:
                    with open(file_path, 'r', encoding=enc) as f:
                        sample = f.read(8192)
                    sniffer = csv.Sniffer()
                    dialect = sniffer.sniff(sample, delimiters='|;\t,~^\x01')
                    return dialect.delimiter
                except:
                    continue
        except:
            pass
        return ','

    def _load_columns(self, file_path: str):
        try:
            suffix = Path(file_path).suffix.lower()
            if suffix == '.csv':
                sep = self._detect_sep(file_path)
                for enc in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                    try:
                        df = pl.read_csv(file_path, encoding=enc, separator=sep, n_rows=1,
                                         ignore_errors=True, has_header=True, null_values=[])
                        break
                    except:
                        continue
                else:
                    df = pl.read_csv(file_path, encoding='utf-8', separator=sep, n_rows=1,
                                     ignore_errors=True, has_header=True, null_values=[])
            elif suffix in {'.xlsx', '.xls'}:
                df = pl.read_excel(file_path, n_rows=1)
                df = df.fill_null("")
            elif suffix == '.xml':
                df = self._read_xml_quick(file_path)
            elif suffix == '.zip':
                df = self._read_zip_quick(file_path)
            else:
                QMessageBox.warning(self, 'Error', f'Unsupported format: {suffix}')
                self.reject()
                return
            self.all_columns = df.columns
            self._populate_list(self.all_columns)
        except Exception as e:
            QMessageBox.warning(self, 'Error', f'Failed to read file:\n{str(e)}')
            self.reject()

    def _read_xml_quick(self, file_path: str) -> pl.DataFrame:
        try:
            import pandas as pd
            tree = ET.parse(file_path)
            root = tree.getroot()
            items = []
            for tag in ['item', 'product', 'offer', 'record', 'row']:
                items = root.findall(f'.//{tag}')
                if items:
                    break
            if not items:
                items = list(root)
            records = []
            for item in items[:5]:
                record = {}
                for child in item:
                    if len(list(child)) > 0:
                        for subchild in child:
                            record[f"{child.tag}_{subchild.tag}"] = subchild.text.strip() if subchild.text else ''
                    else:
                        record[child.tag] = child.text.strip() if child.text else ''
                for key, val in item.attrib.items():
                    record[f"@{key}"] = val
                records.append(record)
            pdf = pd.DataFrame(records).fillna('')
            pdf = pdf.fillna('').astype(str)
            return pl.from_pandas(pdf)
        except:
            return pl.DataFrame()

    def _read_zip_quick(self, file_path: str) -> pl.DataFrame:
        try:
            with zipfile.ZipFile(file_path, 'r') as zf:
                for name in zf.namelist():
                    ext = Path(name).suffix.lower()
                    if ext == '.csv':
                        with zf.open(name) as f:
                            sep = self._detect_sep_from_bytes(f.read())
                            f.seek(0)
                            data = BytesIO(f.read())
                            df = pl.read_csv(data, has_header=True, separator=sep, n_rows=1, ignore_errors=True)
                            return df
                    elif ext in {'.xlsx', '.xls'}:
                        with zf.open(name) as f:
                            data = BytesIO(f.read())
                            df = pl.read_excel(data, n_rows=1)
                            return df.fill_null("")
                    elif ext == '.xml':
                        with zf.open(name) as f:
                            data = f.read()
                            try:
                                import pandas as pd
                                tree = ET.parse(BytesIO(data))
                                root = tree.getroot()
                                items = []
                                for tag in ['item', 'product', 'offer', 'record', 'row']:
                                    items = root.findall(f'.//{tag}')
                                    if items:
                                        break
                                if not items:
                                    items = list(root)
                                records = []
                                for item in items[:5]:
                                    record = {}
                                    for child in item:
                                        if len(list(child)) > 0:
                                            for subchild in child:
                                                record[f"{child.tag}_{subchild.tag}"] = subchild.text.strip() if subchild.text else ''
                                        else:
                                            record[child.tag] = child.text.strip() if child.text else ''
                                    for key, val in item.attrib.items():
                                        record[f"@{key}"] = val
                                    records.append(record)
                                pdf = pd.DataFrame(records).fillna('')
                                pdf = pdf.fillna('').astype(str)
                                return pl.from_pandas(pdf)
                            except:
                                return pl.DataFrame()
            return pl.DataFrame()
        except:
            return pl.DataFrame()

    def _detect_sep_from_bytes(self, data: bytes) -> str:
        try:
            text = data.decode('utf-8', errors='ignore')
            sample = text[:8192]
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters='|;\t,~^\x01')
            return dialect.delimiter
        except:
            return ','

    def _populate_list(self, columns: List[str]):
        self.column_list.clear()
        for col in columns:
            item = QListWidgetItem()
            checkbox = QCheckBox(col)
            checkbox.setChecked(col in self.currently_selected or not self.currently_selected)
            checkbox.stateChanged.connect(self._update_stats)
            self.column_list.addItem(item)
            self.column_list.setItemWidget(item, checkbox)
        self._update_stats()

    def _filter_columns(self, text: str):
        search_text = text.lower().strip()
        if not search_text:
            self._populate_list(self.all_columns)
            return
        filtered = [col for col in self.all_columns if search_text in col.lower()]
        self._populate_list(filtered)

    def _toggle_all(self, checked: bool):
        for i in range(self.column_list.count()):
            checkbox = self.column_list.itemWidget(self.column_list.item(i))
            if checkbox:
                checkbox.setChecked(checked)
        self._update_stats()

    def _update_stats(self):
        total = self.column_list.count()
        checked = sum(1 for i in range(total) if self.column_list.itemWidget(self.column_list.item(i)).isChecked())
        self.stats_label.setText(f"Selected: {checked} / {total} columns")

    def _apply_selection(self):
        self.selected_columns = []
        for i in range(self.column_list.count()):
            checkbox = self.column_list.itemWidget(self.column_list.item(i))
            if checkbox and checkbox.isChecked():
                self.selected_columns.append(checkbox.text())
        self.accept()

    def get_selected_columns(self) -> List[str]:
        return self.selected_columns


# ============================================================================
# Completion Dialog
# ============================================================================

class CompletionDialog(QDialog):
    def __init__(self, parent, title, summary_data):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(550)
        self.setStyleSheet("""
            QDialog { background-color: white; }
            QLabel { color: #1a1a2e; font-size: 13px; padding: 5px; }
            QLabel#titleLabel { font-size: 18px; font-weight: bold; color: #1a1a2e; }
            QPushButton { background-color: #4096ff; color: white; border: none; padding: 10px 24px; border-radius: 6px; font-weight: bold; font-size: 13px; }
            QPushButton:hover { background-color: #1677ff; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 20, 20, 20)

        title_label = QLabel("✅ Validation Complete!")
        title_label.setObjectName("titleLabel")
        layout.addWidget(title_label)

        total = summary_data.get('total_pairs', 0)
        success = summary_data.get('files', 0)
        skipped = summary_data.get('skipped', 0)
        failed = summary_data.get('failed', 0)

        info_text = f"""
<b>Files Processed:</b> {total}<br>
<b>✅ Successfully Compared:</b> {success}<br>
<b>⏭️ Skipped (no SKU column):</b> {skipped}<br>
<b>❌ Failed:</b> {failed}<br>
<br>
<b>Overall Match Rate:</b> {summary_data.get('match_rate', 0)}%<br>
<b>Total Differences:</b> {summary_data.get('diffs', 0):,}<br>
<br>
<i>💡 Check the "Debug Log" tab for details!</i>
        """
        info_label = QLabel(info_text)
        info_label.setWordWrap(True)
        layout.addWidget(info_label)

        btn_layout = QHBoxLayout()
        view_btn = QPushButton("View Results")
        view_btn.clicked.connect(self.accept)
        btn_layout.addWidget(view_btn)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        btn_layout.addWidget(close_btn)
        layout.addLayout(btn_layout)


def show_message(parent, title, message, icon=QMessageBox.Information):
    msg_box = QMessageBox(parent)
    msg_box.setWindowTitle(title)
    msg_box.setText(message)
    msg_box.setIcon(icon)
    msg_box.setStyleSheet("""
        QMessageBox { background-color: white; color: #1a1a2e; }
        QMessageBox QLabel { color: #1a1a2e; font-size: 13px; min-width: 300px; padding: 10px; }
        QPushButton { background-color: #4096ff; color: white; border: none; padding: 8px 20px; border-radius: 4px; font-weight: bold; min-width: 80px; }
        QPushButton:hover { background-color: #1677ff; }
    """)
    return msg_box.exec()


# ============================================================================
# Column Match Statistics (with streaming to CSV)
# ============================================================================

class ColumnMatchStats:
    @staticmethod
    def _values_equal(v1, v2) -> bool:
        if (v1 == '' or v1 is None) and (v2 == '' or v2 is None):
            return True
        if v1 == v2:
            return True
        try:
            n1 = float(v1)
            n2 = float(v2)
            return n1 == n2
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _is_empty(val) -> bool:
        """Check if a value is empty (None, NaN, or empty string)."""
        if val is None:
            return True
        if isinstance(val, float) and np.isnan(val):
            return True
        if val == '':
            return True
        return False

    @staticmethod
    def calculate(df1_common: pl.DataFrame, df2_common: pl.DataFrame, sku_col1, sku_col2,
                  compare_cols: List[str], common_skus, diff_csv_path: str) -> Dict:
        """
        Compare columns and stream differences directly to a CSV file.
        Returns stats without mismatch_details (empty list).
        """
        stats = {}
        skus = df1_common['_sku_norm'].to_list()

        with open(diff_csv_path, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['sku', 'column', 'value_file1', 'value_file2'])

            for col in compare_cols:
                if col not in df1_common.columns or col not in df2_common.columns:
                    continue

                vals1 = df1_common[col].to_list()
                vals2 = df2_common[col].to_list()

                total_compared = 0
                exact_matches = 0
                mismatches = 0
                both_nan = 0
                only_file1_nan = 0
                only_file2_nan = 0

                for idx in range(len(vals1)):
                    v1 = vals1[idx]
                    v2 = vals2[idx]
                    total_compared += 1

                    is_v1_empty = ColumnMatchStats._is_empty(v1)
                    is_v2_empty = ColumnMatchStats._is_empty(v2)

                    if is_v1_empty and is_v2_empty:
                        both_nan += 1
                        exact_matches += 1
                    elif is_v1_empty:
                        only_file1_nan += 1
                        mismatches += 1
                        writer.writerow([str(skus[idx]), col, 'EMPTY', str(v2)])
                    elif is_v2_empty:
                        only_file2_nan += 1
                        mismatches += 1
                        writer.writerow([str(skus[idx]), col, str(v1), 'EMPTY'])
                    else:
                        if ColumnMatchStats._values_equal(v1, v2):
                            exact_matches += 1
                        else:
                            mismatches += 1
                            writer.writerow([str(skus[idx]), col, str(v1), str(v2)])

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
                    'only_file1_nan': only_file1_nan,
                    'only_file2_nan': only_file2_nan,
                    'match_rate': round(match_rate, 2),
                    'status': status,
                    'mismatch_details': []
                }
        return stats

    @staticmethod
    def calculate_overall(all_column_stats):
        overall = defaultdict(lambda: {
            'total_compared': 0, 'exact_matches': 0, 'mismatches': 0,
            'both_nan': 0, 'only_file1_nan': 0, 'only_file2_nan': 0,
            'file_count': 0
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


# ============================================================================
# Debug Logger
# ============================================================================

class DebugLogger:
    def __init__(self):
        self.logs = []
        self.lock = threading.Lock()
        self.enabled = True
        self.current_file = ""

    def set_current_file(self, filename: str):
        with self.lock:
            self.current_file = filename

    def log(self, category: str, message: str, filename: str = ""):
        if not self.enabled:
            return
        timestamp = datetime.now().strftime('%H:%M:%S.%f')[:-3]
        file_context = filename or self.current_file
        with self.lock:
            self.logs.append({'time': timestamp, 'category': category, 'file': file_context, 'message': message})

    def info(self, msg, filename=""): self.log('INFO', msg, filename)
    def warn(self, msg, filename=""): self.log('WARN', msg, filename)
    def error(self, msg, filename=""): self.log('ERROR', msg, filename)
    def match(self, msg, filename=""): self.log('MATCH', msg, filename)
    def miss(self, msg, filename=""): self.log('MISS', msg, filename)
    def debug(self, msg, filename=""): self.log('DEBUG', msg, filename)
    def success(self, msg, filename=""): self.log('SUCCESS', msg, filename)
    def stat(self, msg, filename=""): self.log('STAT', msg, filename)
    def detect(self, msg, filename=""): self.log('DETECT', msg, filename)
    def skip(self, msg, filename=""): self.log('SKIP', msg, filename)

    def get_logs(self, categories=None):
        with self.lock:
            if categories:
                return [l for l in self.logs if l['category'] in categories]
            return self.logs.copy()

    def clear(self):
        with self.lock:
            self.logs.clear()
            self.current_file = ""

    def get_summary(self):
        with self.lock:
            summary = {}
            for log in self.logs:
                cat = log['category']
                summary[cat] = summary.get(cat, 0) + 1
            return summary


# ============================================================================
# SKU Normalizer
# ============================================================================

class SKUNormalizer:
    def normalize(self, sku):
        if sku is None or (isinstance(sku, float) and np.isnan(sku)):
            return ''
        s = str(sku).strip()
        s = unicodedata.normalize('NFKC', s)
        for c in ['\u00A0', '\u2007', '\u202F']:
            s = s.replace(c, ' ')
        for c in ['\u2013', '\u2014', '\u2015']:
            s = s.replace(c, '-')
        s = re.sub(r'[\t\n\r\f\v]', '', s)
        s = re.sub(r'\s+', ' ', s)
        cleaned = s.replace('.', '').replace('-', '').replace(' ', '')
        if cleaned.isdigit() and len(cleaned) > 1:
            try:
                if '.' in s:
                    num = float(s)
                    if num == int(num):
                        s = str(int(num))
                else:
                    s = str(int(s))
            except ValueError:
                pass
        return s

    def normalize_series(self, series: pl.Series) -> pl.Series:
        return series.map_elements(self.normalize, return_dtype=pl.String)


# ============================================================================
# Fast Batch Validator (Polars backend, streaming diffs)
# ============================================================================

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
        self.max_workers = max_workers
        self.normalize_sku = normalize_sku
        self.case_sensitive = case_sensitive
        self.debug = debug
        self.auto_detect_sku = auto_detect_sku
        self.exclude_keywords = exclude_keywords or []
        self.temp_diff_dir = temp_diff_dir or Path(tempfile.gettempdir()) / "feed_validator_diffs"
        self.temp_diff_dir.mkdir(exist_ok=True)
        self.lock = threading.Lock()
        self.logger = DebugLogger()
        self.logger.enabled = debug
        self.normalizer = SKUNormalizer()
        self.sku_detector = SKUColumnDetector(
            user_specified=sku_column if sku_column else None,
            priority_list=priority_list
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
        self.logger.info(f"Supported formats: CSV, Excel (.xlsx/.xls), XML, ZIP (containing CSV/Excel/XML)", filename="SYSTEM")
        if not self.folder1.exists():
            raise FileNotFoundError(f"Folder not found: {self.folder1}")
        if not self.folder2.exists():
            raise FileNotFoundError(f"Folder not found: {self.folder2}")
        self.matching_files = self._find_files()
        self.logger.info(f"Matching file pairs: {len(self.matching_files)}", filename="SYSTEM")
        for f in self.matching_files:
            self.logger.debug(f"  📄 {f}", filename="SYSTEM")

    def _find_files(self):
        exts = {'.csv', '.xlsx', '.xls', '.xml', '.zip'}
        if self.file_pattern:
            f1 = {f.name for f in self.folder1.glob(self.file_pattern)}
            f2 = {f.name for f in self.folder2.glob(self.file_pattern)}
        else:
            f1 = {f.name for f in self.folder1.iterdir() if f.suffix.lower() in exts and f.is_file()}
            f2 = {f.name for f in self.folder2.iterdir() if f.suffix.lower() in exts and f.is_file()}
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

    def _detect_separator(self, file_path: Path) -> str:
        """Auto-detect CSV delimiter using csv.Sniffer."""
        try:
            for enc in ['utf-8', 'gbk', 'gb2312', 'latin-1']:
                try:
                    with open(file_path, 'r', encoding=enc) as f:
                        sample = f.read(8192)
                    sniffer = csv.Sniffer()
                    dialect = sniffer.sniff(sample, delimiters='|;\t,~^\x01')
                    return dialect.delimiter
                except:
                    continue
        except:
            pass
        return ','

    def _detect_separator_from_bytes(self, data: bytes) -> str:
        """Auto-detect CSV delimiter from bytes using csv.Sniffer."""
        try:
            text = data.decode('utf-8', errors='ignore')
            sample = text[:8192]
            sniffer = csv.Sniffer()
            dialect = sniffer.sniff(sample, delimiters='|;\t,~^\x01')
            return dialect.delimiter
        except:
            return ','

    def _read_csv_with_retry(self, file_path: Path, is_zip=False, zip_file=None, csv_name=None) -> pl.DataFrame:
        encodings = ['utf-8', 'gbk', 'gb2312', 'latin-1']
        if is_zip:
            with zip_file.open(csv_name) as f:
                raw_bytes = f.read()
            sep = self._detect_separator_from_bytes(raw_bytes)
            reader = lambda enc, sep: pl.read_csv(BytesIO(raw_bytes), encoding=enc, separator=sep,
                                                  ignore_errors=True, has_header=True, null_values=[],
                                                  truncate_ragged_lines=True)
        else:
            sep = self._detect_separator(file_path)
            reader = lambda enc, sep: pl.read_csv(file_path, encoding=enc, separator=sep,
                                                  ignore_errors=True, has_header=True, null_values=[],
                                                  truncate_ragged_lines=True)
        df = None
        last_error = None
        for enc in encodings:
            try:
                df = reader(enc, sep)
                break
            except Exception as e:
                last_error = e
                continue
        if df is None:
            raise ValueError(f"Cannot read CSV: {file_path} - {last_error}")
        if df.shape[1] == 1:
            self.logger.debug(f"  Only 1 column detected, retrying all delimiters...", filename=self.logger.current_file)
            delimiters = ['|', '\t', ';', ',', '~', '^']
            best_df = df
            best_cols = 1
            for delim in delimiters:
                if delim == sep: continue
                for enc in encodings:
                    try:
                        df2 = reader(enc, delim)
                        if df2.shape[1] > best_cols:
                            best_df = df2
                            best_cols = df2.shape[1]
                        break
                    except: continue
            df = best_df
        return df

    def _read_xml_file(self, file_path: Path) -> pl.DataFrame:
        self.logger.debug(f"  Parsing XML file...", filename=self.logger.current_file)

        # Read raw bytes
        with open(file_path, 'rb') as f:
            raw_bytes = f.read()
        
        # Remove BOM if present
        if raw_bytes[:3] == b'\xef\xbb\xbf':
            raw_bytes = raw_bytes[3:]
        
        # Decode with replacement
        text = raw_bytes.decode('utf-8', errors='replace')
        
        # Remove XML declarations completely to avoid encoding switching
        text = re.sub(r'<\?xml[^?]*\?>', '', text, count=1, flags=re.DOTALL | re.IGNORECASE)
        
        # Remove illegal XML characters
        illegal_pattern = re.compile(
            '[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]'
        )
        text = illegal_pattern.sub('', text)
        
        # Encode back to UTF-8 bytes
        xml_bytes = text.encode('utf-8')
        
        # Try standard library first (no encoding detection issues)
        try:
            root = ET.fromstring(xml_bytes)
            tree = ET.ElementTree(root)
            self.logger.debug(f"  Using ElementTree parser", filename=self.logger.current_file)
        except ET.ParseError:
            # Try lxml XML parser with recover mode
            try:
                import lxml.etree as lxml_etree
                xml_parser = lxml_etree.XMLParser(recover=True, huge_tree=True)
                root = lxml_etree.fromstring(xml_bytes, parser=xml_parser)
                tree = lxml_etree.ElementTree(root)
                self.logger.debug(f"  Using lxml XML parser (recover mode)", filename=self.logger.current_file)
            except ImportError:
                raise
            except Exception:
                # Last resort: try lxml HTML parser
                try:
                    import lxml.etree as lxml_etree
                    html_parser = lxml_etree.HTMLParser(recover=True)
                    root = lxml_etree.fromstring(xml_bytes, parser=html_parser)
                    if root.tag == 'html':
                        body = root.find('body')
                        if body is not None and len(body) > 0:
                            root = body
                    tree = lxml_etree.ElementTree(root)
                    self.logger.debug(f"  Using lxml HTML parser (last resort)", filename=self.logger.current_file)
                except Exception:
                    raise

        root = tree.getroot()

        # Tag cleaning helper: removes both namespace URIs {uri} and namespace prefixes ns:
        def clean_tag(tag: str) -> str:
            # Remove {namespace} URI
            if '}' in tag:
                tag = tag.split('}', 1)[1]
            # Remove namespace prefix like g: or c:
            if ':' in tag:
                parts = tag.split(':')
                if len(parts[0]) <= 3:
                    tag = parts[-1]
            return tag

        # Find repeating elements
        items = []
        for tag in ['item', 'product', 'offer', 'record', 'row', 'entry']:
            items = root.findall(f'.//{tag}')
            if not items:
                items = root.findall(f'.//{{*}}{tag}')
            if items:
                self.logger.debug(f"  Found {len(items)} <{tag}> elements", filename=self.logger.current_file)
                break
        if not items:
            items = list(root)
            if items:
                tag_name = clean_tag(items[0].tag) if items else 'unknown'
                self.logger.debug(f"  Using {len(items)} direct children (<{tag_name}>)", filename=self.logger.current_file)
        if not items:
            raise ValueError("Could not find repeating data elements in XML")

        # Extract records with consistent tag cleaning
        records = []
        for item in items:
            record = {}
            for child in item:
                child_tag = clean_tag(child.tag)
                children = list(child)
                if children:
                    for subchild in children:
                        sub_tag = clean_tag(subchild.tag)
                        key = f"{child_tag}_{sub_tag}"
                        record[key] = subchild.text.strip() if subchild.text else ''
                else:
                    record[child_tag] = child.text.strip() if child.text else ''
            
            # Clean attribute keys too
            for key, val in item.attrib.items():
                clean_key = clean_tag(key)
                record[f"@{clean_key}"] = val
            
            records.append(record)

        if not records:
            raise ValueError("No data extracted from XML")

        import pandas as pd
        pdf = pd.DataFrame(records).fillna('')
        df = pl.from_pandas(pdf)
        self.logger.debug(f"  XML converted: {len(df)} rows, {len(df.columns)} cols", filename=self.logger.current_file)
        return df

    def _read_zip_file(self, file_path: Path) -> pl.DataFrame:
        """Read the first supported file from a ZIP archive (CSV, Excel, XML)"""
        self.logger.debug(f"  Opening ZIP file: {file_path.name}", filename=self.logger.current_file)
        with zipfile.ZipFile(file_path, 'r') as zf:
            for name in zf.namelist():
                ext = Path(name).suffix.lower()
                if ext == '.csv':
                    self.logger.debug(f"  Reading CSV '{name}' from ZIP", filename=self.logger.current_file)
                    return self._read_csv_with_retry(file_path, is_zip=True, zip_file=zf, csv_name=name)
                elif ext in {'.xlsx', '.xls'}:
                    self.logger.debug(f"  Reading Excel '{name}' from ZIP", filename=self.logger.current_file)
                    with zf.open(name) as f:
                        data = BytesIO(f.read())
                        df = pl.read_excel(data)
                        df = df.fill_null("")
                        return df
                elif ext == '.xml':
                    self.logger.debug(f"  Reading XML '{name}' from ZIP", filename=self.logger.current_file)
                    with zf.open(name) as f:
                        data = f.read()
                    try:
                        import lxml.etree as lxml_etree
                        tree = lxml_etree.parse(BytesIO(data))
                        root = tree.getroot()
                    except ImportError:
                        tree = ET.parse(BytesIO(data))
                        root = tree.getroot()
                    items = []
                    for tag in ['item', 'product', 'offer', 'record', 'row', 'entry']:
                        items = root.findall(f'.//{tag}')
                        if items:
                            break
                    if not items:
                        items = list(root)
                    records = []
                    for item in items:
                        record = {}
                        for child in item:
                            child_tag = child.tag.split('}', 1)[-1] if '}' in child.tag else child.tag
                            children = list(child)
                            if children:
                                for subchild in children:
                                    sub_tag = subchild.tag.split('}', 1)[-1] if '}' in subchild.tag else subchild.tag
                                    key = f"{child_tag}_{sub_tag}"
                                    record[key] = subchild.text.strip() if subchild.text else ''
                            else:
                                record[child_tag] = child.text.strip() if child.text else ''
                        for key, val in item.attrib.items():
                            clean_key = key.split('}', 1)[-1] if '}' in key else key
                            record[f"@{clean_key}"] = val
                        records.append(record)
                    import pandas as pd
                    pdf = pd.DataFrame(records).fillna('')
                    pdf = pdf.fillna('').astype(str)
                    return pl.from_pandas(pdf)
            raise ValueError(f"No supported file (CSV/Excel/XML) found inside ZIP: {file_path.name}")

    def _read_file(self, file_path: Path) -> pl.DataFrame:
        suffix = file_path.suffix.lower()
        if suffix == '.xml':
            return self._read_xml_file(file_path)
        elif suffix == '.zip':
            return self._read_zip_file(file_path)
        elif suffix == '.csv':
            df = self._read_csv_with_retry(file_path)
        elif suffix in {'.xlsx', '.xls'}:
            df = pl.read_excel(file_path)
            df = df.fill_null("")
        else:
            raise ValueError(f"Unsupported format: {suffix}")
        self.logger.debug(f"  Read OK: rows={len(df):,}, cols={len(df.columns)}", filename=self.logger.current_file)
        return df
    def _align_dataframes(self, df1, df2, common_norm, filename):
        """Align two DataFrames by common SKUs using Polars join."""
        df1_filtered = df1.filter(pl.col('_sku_norm').is_in(list(common_norm)))
        df2_filtered = df2.filter(pl.col('_sku_norm').is_in(list(common_norm)))
        
        dup_count_1 = df1_filtered.shape[0] - df1_filtered['_sku_norm'].n_unique()
        dup_count_2 = df2_filtered.shape[0] - df2_filtered['_sku_norm'].n_unique()
        
        if dup_count_1 > 0 or dup_count_2 > 0:
            self.logger.debug(
                f"  Duplicate SKUs: F1={dup_count_1}, F2={dup_count_2}. Keeping first.",
                filename=filename
            )
        
        df1_unique = df1_filtered.unique(subset=['_sku_norm'], keep='first')
        df2_unique = df2_filtered.unique(subset=['_sku_norm'], keep='first')
        
        df1_common = df1_unique.join(
            df2_unique.select(['_sku_norm']),
            on='_sku_norm',
            how='inner'
        ).sort('_sku_norm')
        
        df2_common = df2_unique.join(
            df1_common.select(['_sku_norm']),
            on='_sku_norm',
            how='inner'
        ).sort('_sku_norm')
        
        self.logger.debug(
            f"  Aligned: {len(df1_common)} common rows",
            filename=filename
        )
        
        return df1_common, df2_common

    def _validate_single_pair(self, filename):
        self.logger.set_current_file(filename)
        file1_path = self.folder1 / filename
        file2_path = self.folder2 / filename
        self.logger.info(f"{'='*70}", filename=filename)
        self.logger.info(f"📄 START PROCESSING", filename=filename)
        self.logger.info(f"  Baseline : {file1_path.name} ({file1_path.suffix.upper()})", filename=filename)
        self.logger.info(f"  Comparison: {file2_path.name} ({file2_path.suffix.upper()})", filename=filename)
        try:
            df1 = self._read_file(file1_path)
            df2 = self._read_file(file2_path)
            self.logger.info(f"  File 1: {len(df1):,} rows, {len(df1.columns)} cols", filename=filename)
            self.logger.info(f"  File 2: {len(df2):,} rows, {len(df2.columns)} cols", filename=filename)
            sku_col1 = None
            sku_col2 = None
            if self.auto_detect_sku:
                self.logger.detect(f"🔍 Auto-detecting SKU column...", filename=filename)
                if self.user_sku_column:
                    if self.user_sku_column in df1.columns and self.user_sku_column in df2.columns:
                        sku_col1 = self.user_sku_column
                        sku_col2 = self.user_sku_column
                        self.logger.detect(f"✅ Using user-specified: '{sku_col1}'", filename=filename)
                if sku_col1 is None:
                    sku_col1, sku_col2, confidence = self.sku_detector.detect_common_sku(
                        df1.columns, df2.columns, filename
                    )
                    if sku_col1 is None:
                        self.logger.skip(f"⏭️ SKIPPED: No SKU column detected", filename=filename)
                        return {'status': 'skipped', 'file': filename, 'reason': 'No SKU column detected'}
                    if confidence < 0.7:
                        self.logger.warn(f"⚠️ Low confidence ({confidence:.2f})", filename=filename)
            else:
                if self.user_sku_column:
                    if self.user_sku_column in df1.columns and self.user_sku_column in df2.columns:
                        sku_col1 = self.user_sku_column
                        sku_col2 = self.user_sku_column
                    else:
                        return {'status': 'skipped', 'file': filename, 'reason': f"SKU column not found"}
                else:
                    return {'status': 'error', 'file': filename, 'error': 'No SKU column specified'}
            self.logger.match(f"🎯 SKU column File1: '{sku_col1}'", filename=filename)
            self.logger.match(f"🎯 SKU column File2: '{sku_col2}'", filename=filename)

            df1 = df1.with_columns(pl.col(sku_col1).cast(pl.String).alias(sku_col1))
            df2 = df2.with_columns(pl.col(sku_col2).cast(pl.String).alias(sku_col2))
            raw_skus1 = df1.filter(pl.col(sku_col1) != '')[sku_col1].unique().to_list()
            raw_skus2 = df2.filter(pl.col(sku_col2) != '')[sku_col2].unique().to_list()

            if self.normalize_sku:
                norm_set1, norm_set2 = set(), set()
                norm_map1, norm_map2 = {}, {}
                for sku in raw_skus1:
                    s = str(sku).strip()
                    n = self.normalizer.normalize(s)
                    if not self.case_sensitive: n = n.lower()
                    if n:
                        norm_set1.add(n)
                        if n not in norm_map1: norm_map1[n] = s
                for sku in raw_skus2:
                    s = str(sku).strip()
                    n = self.normalizer.normalize(s)
                    if not self.case_sensitive: n = n.lower()
                    if n:
                        norm_set2.add(n)
                        if n not in norm_map2: norm_map2[n] = s
            else:
                norm_set1 = set(str(s).strip() for s in raw_skus1)
                norm_set2 = set(str(s).strip() for s in raw_skus2)
                if not self.case_sensitive:
                    norm_set1 = {s.lower() for s in norm_set1}
                    norm_set2 = {s.lower() for s in norm_set2}
                norm_map1 = {s: s for s in norm_set1}
                norm_map2 = {s: s for s in norm_set2}

            common_norm = norm_set1 & norm_set2
            only_1 = norm_set1 - norm_set2
            only_2 = norm_set2 - norm_set1

            df1 = df1.with_columns(
                self.normalizer.normalize_series(pl.col(sku_col1)).alias('_sku_norm')
            )
            df2 = df2.with_columns(
                self.normalizer.normalize_series(pl.col(sku_col2)).alias('_sku_norm')
            )
            if not self.case_sensitive:
                df1 = df1.with_columns(pl.col('_sku_norm').str.to_lowercase())
                df2 = df2.with_columns(pl.col('_sku_norm').str.to_lowercase())

            df1_common, df2_common = self._align_dataframes(df1, df2, common_norm, filename)

            if self.compare_columns:
                valid_cols = [c for c in self.compare_columns if c in df1.columns and c in df2.columns]
            else:
                common_cols = set(df1.columns) & set(df2.columns) - {sku_col1, sku_col2, '_sku_norm'}
                valid_cols = sorted(common_cols)

            if self.exclude_keywords:
                excluded_cols = [c for c in valid_cols if any(kw.lower() in c.lower() for kw in self.exclude_keywords)]
                valid_cols = [c for c in valid_cols if not any(kw.lower() in c.lower() for kw in self.exclude_keywords)]
                if excluded_cols:
                    self.logger.debug(f"  Excluded columns: {excluded_cols}", filename=filename)

            safe_name = re.sub(r'[^\w\-]', '_', filename)
            diff_csv = self.temp_diff_dir / f"diff_{safe_name}_{threading.get_ident()}.csv"

            column_stats = ColumnMatchStats.calculate(
                df1_common, df2_common, sku_col1, sku_col2, valid_cols, common_norm, str(diff_csv)
            )

            total_diffs = sum(s['mismatches'] for s in column_stats.values())
            avg_match_rate = sum(s['match_rate'] for s in column_stats.values()) / len(column_stats) if column_stats else 0

            del df1, df2, df1_common, df2_common
            gc.collect()

            return {
                'status': 'success', 'file': filename, 'sku_col1': sku_col1, 'sku_col2': sku_col2,
                'summary': {
                    'rows_file1': len(raw_skus1), 'rows_file2': len(raw_skus2),
                    'unique_skus_file1': len(norm_set1), 'unique_skus_file2': len(norm_set2),
                    'common_skus': len(common_norm), 'only_in_file1': len(only_1), 'only_in_file2': len(only_2),
                    'total_differences': total_diffs, 'avg_match_rate': round(avg_match_rate, 1),
                    'columns_compared': len(valid_cols)
                },
                'column_stats': column_stats, 'diff_csv_path': str(diff_csv),
                'missing_in_file1': sorted([norm_map2.get(s, s) for s in only_2]),
                'missing_in_file2': sorted([norm_map1.get(s, s) for s in only_1])
            }
        except Exception as e:
            self.logger.error(f"❌ ERROR: {str(e)}", filename=filename)
            self.logger.debug(f"  Traceback: {traceback.format_exc()}", filename=filename)
            return {'status': 'error', 'file': filename, 'error': str(e)}

    def validate_all_parallel(self, progress_callback=None):
        total = len(self.matching_files)
        completed = 0
        self.logger.info(f"🚀 STARTING VALIDATION: {total} files, {self.max_workers} workers", filename="SYSTEM")
        all_results = {
            'files': {}, 'errors': [], 'skipped': [],
            'overall': {'total_pairs': total, 'success': 0, 'failed': 0, 'skipped': 0,
                        'total_skus': 0, 'total_diffs': 0, 'total_missing': 0, 'avg_match_rate': 0},
            'overall_column_stats': {}, 'debug_logs': [], 'sku_detection_log': [],
            'temp_diff_dir': str(self.temp_diff_dir)
        }
        all_column_stats = []
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_file = {executor.submit(self._validate_single_pair, f): f for f in self.matching_files}
            for future in as_completed(future_to_file):
                filename = future_to_file[future]
                completed += 1
                try:
                    result = future.result()
                    all_results['files'][filename] = result
                    if result['status'] == 'success':
                        all_results['overall']['success'] += 1
                        s = result['summary']
                        all_results['overall']['total_skus'] += s['common_skus']
                        all_results['overall']['total_diffs'] += s['total_differences']
                        all_results['overall']['total_missing'] += s['only_in_file1'] + s['only_in_file2']
                        if 'column_stats' in result:
                            all_column_stats.append(result['column_stats'])
                    elif result['status'] == 'skipped':
                        all_results['overall']['skipped'] += 1
                        all_results['skipped'].append({'file': filename, 'reason': result.get('reason', 'Unknown')})
                    else:
                        all_results['overall']['failed'] += 1
                        all_results['errors'].append({'file': filename, 'error': result.get('error', 'Unknown')})
                except Exception as e:
                    all_results['overall']['failed'] += 1
                    all_results['errors'].append({'file': filename, 'error': str(e)})

                if completed % 2 == 0:
                    gc.collect()
                if progress_callback:
                    progress_callback(completed, total, filename)

        gc.collect()
        if all_column_stats:
            all_results['overall_column_stats'] = ColumnMatchStats.calculate_overall(all_column_stats)
            rates = [s['match_rate'] for s in all_results['overall_column_stats'].values()]
            all_results['overall']['avg_match_rate'] = round(sum(rates) / len(rates), 1) if rates else 0
        all_results['debug_logs'] = self.logger.get_logs()
        all_results['log_summary'] = self.logger.get_summary()
        all_results['sku_detection_log'] = self.sku_detector.get_log()
        return all_results


# ============================================================================
# API Workers
# ============================================================================

def _load_config():
    config_path = Path(__file__).parent / "platform_config.json"
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {"auth_token": "", "base_url": "https://platform-api.productsup.io/platform/v2"}

_config = _load_config()
HEADERS = {"X-Auth-Token": _config["auth_token"]}
BASE_URL = _config["base_url"]

class FetchProjectsWorker(QThread):
    log = Signal(str)
    finished = Signal(list)
    error = Signal(str)
    def run(self):
        try:
            self.log.emit("Fetching project list...")
            resp = requests.get(f"{BASE_URL}/projects", headers=HEADERS, verify=False)
            if resp.status_code != 200 or not resp.json().get("success"):
                self.error.emit("Failed to fetch projects")
                return
            projects = resp.json()["Projects"]
            project_list = [{"id": p["id"], "name": p["name"]} for p in projects]
            self.log.emit(f"Loaded {len(project_list)} projects.")
            self.finished.emit(project_list)
        except Exception as e:
            self.error.emit(str(e))

class FetchSitesWorker(QThread):
    log = Signal(str)
    finished = Signal(list)
    error = Signal(str)
    def __init__(self, project_id, only_active=False):
        super().__init__()
        self.project_id = project_id
        self.only_active = only_active
    def run(self):
        try:
            self.log.emit("Fetching sites...")
            resp = requests.get(f"{BASE_URL}/projects/{self.project_id}/sites", headers=HEADERS, verify=False)
            if resp.status_code != 200 or not resp.json().get("success"):
                self.error.emit("Failed to fetch sites")
                return
            sites = resp.json()["Sites"]
            if self.only_active:
                sites = [s for s in sites if s.get("status") == "active"]
            seen = set()
            unique_sites = []
            for s in sites:
                sid = s["id"]
                if sid not in seen:
                    seen.add(sid)
                    site_title = s.get("title") or s.get("name", f"Site_{sid}")
                    unique_sites.append({"id": sid, "name": site_title, "status": s.get("status", "unknown")})
            self.log.emit(f"Fetched {len(unique_sites)} unique sites.")
            self.finished.emit(unique_sites)
        except Exception as e:
            self.error.emit(str(e))

class FetchChannelsWorker(QThread):
    log = Signal(str)
    finished = Signal(list)
    error = Signal(str)
    def __init__(self, site_id):
        super().__init__()
        self.site_id = site_id
    def run(self):
        try:
            self.log.emit("Fetching channels...")
            resp = requests.get(f"{BASE_URL}/sites/{self.site_id}/channels", headers=HEADERS, verify=False)
            if resp.status_code != 200 or not resp.json().get("success"):
                self.error.emit("Failed to fetch channels")
                return
            channels = resp.json()["Channels"]
            channel_list = []
            for ch in channels:
                feed_dest = ch.get("feed_destinations", {})
                link = ""
                if isinstance(feed_dest, dict) and "2" in feed_dest:
                    urls = feed_dest["2"]
                    if isinstance(urls, list):
                        link = urls[0] if urls else ""
                    else:
                        link = str(urls)
                channel_list.append({"name": ch["name"], "link": link})
            self.log.emit(f"Fetched {len(channel_list)} channels.")
            self.finished.emit(channel_list)
        except Exception as e:
            self.error.emit(str(e))

class DownloadAndCompareWorker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str)
    finished = Signal(dict)
    error = Signal(str)
    def __init__(self, site1_name, site1_channels, site2_name, site2_channels,
                 sku_col, compare_cols, normalize_sku, case_sensitive, debug, auto_detect,
                 exclude_keywords=None):
        super().__init__()
        self.site1_name = site1_name
        self.site1_channels = site1_channels
        self.site2_name = site2_name
        self.site2_channels = site2_channels
        self.sku_col = sku_col
        self.compare_cols = compare_cols
        self.normalize_sku = normalize_sku
        self.case_sensitive = case_sensitive
        self.debug = debug
        self.auto_detect = auto_detect
        self.exclude_keywords = exclude_keywords
        self._download_workers = 4

    def _download_with_retry(self, url, save_path, name, max_retries=3):
        for attempt in range(1, max_retries + 1):
            try:
                resp = requests.get(url, headers=HEADERS, timeout=120, verify=False)
                if resp.status_code == 200:
                    save_path.write_bytes(resp.content)
                    self.log.emit(f"  ✅ Downloaded: {name}")
                    return True
                else:
                    self.log.emit(f"  ⚠️ Attempt {attempt}: {name} returned status {resp.status_code}")
            except Exception as e:
                self.log.emit(f"  ⚠️ Attempt {attempt}: {name} error - {str(e)}")
            if attempt < max_retries:
                self.log.emit(f"  Retrying {name} ({attempt}/{max_retries})...")
        self.log.emit(f"  ❌ Failed to download {name} after {max_retries} retries")
        return False

    def _download_channels(self, channels, download_dir, site_name):
        self.log.emit(f"Downloading {len(channels)} channels for {site_name}...")
        download_tasks = []
        for name, link in channels:
            if not link:
                self.log.emit(f"  ⏭️ Skipping {name} (no link)")
                continue
            ext = Path(link.split('?')[0]).suffix or '.csv'
            fname = f"{name}{ext}"
            save_path = download_dir / fname
            download_tasks.append((name, link, save_path))
        if not download_tasks:
            return
        with ThreadPoolExecutor(max_workers=self._download_workers) as pool:
            futures = {pool.submit(self._download_with_retry, link, path, name): name for name, link, path in download_tasks}
            for future in as_completed(futures):
                _ = future.result()
        self.log.emit(f"Finished downloading for {site_name}")

    def run(self):
        try:
            base_dir = Path.cwd() / "api_downloads"
            base_dir.mkdir(exist_ok=True)
            site1_dir = base_dir / self.site1_name.replace(" ", "_")
            site2_dir = base_dir / self.site2_name.replace(" ", "_")
            if site1_dir.exists():
                shutil.rmtree(site1_dir)
            if site2_dir.exists():
                shutil.rmtree(site2_dir)
            site1_dir.mkdir(exist_ok=True)
            site2_dir.mkdir(exist_ok=True)
            self._download_channels(self.site1_channels, site1_dir, self.site1_name)
            self._download_channels(self.site2_channels, site2_dir, self.site2_name)
            self.log.emit("Starting comparison...")
            validator = FastBatchValidator(
                str(site1_dir), str(site2_dir),
                sku_column=self.sku_col, compare_columns=self.compare_cols,
                max_workers=4, normalize_sku=self.normalize_sku,
                case_sensitive=self.case_sensitive, debug=self.debug,
                auto_detect_sku=self.auto_detect, priority_list=None,
                exclude_keywords=self.exclude_keywords
            )
            results = validator.validate_all_parallel(
                progress_callback=lambda c, t, f: self.progress.emit(c, t, f)
            )
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================================
# Main Window
# ============================================================================

class MainWindow(QMainWindow):
    EXCLUDE_COLUMNS_KEYWORDS = ['stock', 'price']
    AUTO_EXPORT_DIR = "reports"
    MERGE_DIFF_FILES = False

    def __init__(self):
        super().__init__()
        self.results = None
        self.selected_columns = []
        self.api_projects = []
        self.api_sites1_all = []
        self.api_sites2_all = []
        self.api_matched_channels = []
        self.channel_data1 = None
        self.channel_data2 = None
        self.worker = None
        self.worker_api = None
        self._site_cache = {}
        self.api_channel_name_map = {}
        self.api_site1_name = ""
        self.api_site2_name = ""
        self._diff_csv_paths = []
        self.init_ui()

    def init_ui(self):
        self.setWindowTitle('Feed Validator - CSV/Excel/XML/ZIP/API Support')
        self.setGeometry(100, 100, 1500, 1000)

        self.setStyleSheet("""
            QMainWindow { background-color: #f0f2f5; }
            QGroupBox { font-weight: bold; border: 2px solid #d0d5dd; border-radius: 8px; margin-top: 8px; padding: 8px; background: white; color: #1a1a2e; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #1a1a2e; }
            QPushButton { background: #4096ff; color: white; border: none; padding: 6px 16px; border-radius: 6px; font-weight: bold; font-size: 12px; }
            QPushButton:hover { background: #1677ff; }
            QPushButton:disabled { background: #d9d9d9; color: #8c8c8c; }
            QPushButton#selectColBtn { background: #52c41a; }
            QPushButton#selectColBtn:hover { background: #389e0d; }
            QLineEdit { padding: 6px; border: 1px solid #d9d9d9; border-radius: 4px; background: white; color: #1a1a2e; }
            QLineEdit:focus { border-color: #4096ff; }
            QComboBox { 
                padding: 6px; 
                border: 1px solid #d9d9d9; 
                border-radius: 4px; 
                background: white; 
                color: #1a1a2e; 
                font-size: 12px; 
            }
            QComboBox::drop-down { 
                width: 24px; 
                border-left: 1px solid #d9d9d9; 
                background: #f5f6fa; 
            }
            QComboBox QAbstractItemView { 
                background: white; 
                color: #1a1a2e; 
                border: 1px solid #d9d9d9; 
                selection-background-color: #e6f4ff; 
                selection-color: #1a1a2e; 
                padding: 4px; 
            }
            QComboBox QAbstractItemView::item { 
                padding: 6px 8px; 
                color: #1a1a2e; 
                min-height: 20px; 
            }
            QComboBox QAbstractItemView::item:hover { 
                background: #f0f5ff; 
            }
            QSpinBox { padding: 6px; border: 1px solid #d9d9d9; border-radius: 4px; background: white; color: #1a1a2e; }
            QTableWidget { border: 1px solid #d9d9d9; border-radius: 4px; background: white; gridline-color: #f0f0f0; color: #1a1a2e; }
            QTableWidget::item { color: #1a1a2e; padding: 4px; }
            QTableWidget::item:selected { background: #e6f4ff; color: #1a1a2e; }
            QHeaderView::section { background: #4096ff; color: white; padding: 6px; font-weight: bold; border: none; }
            QProgressBar { border: 1px solid #d9d9d9; border-radius: 4px; height: 20px; text-align: center; background: white; color: #1a1a2e; }
            QProgressBar::chunk { background: #52c41a; border-radius: 3px; }
            QTabWidget::pane { border: 1px solid #d9d9d9; border-radius: 4px; background: white; }
            QTabBar::tab { background: #f0f0f0; color: #595959; padding: 8px 14px; margin-right: 2px; border-top-left-radius: 4px; border-top-right-radius: 4px; }
            QTabBar::tab:selected { background: white; color: #4096ff; font-weight: bold; }
            QTextEdit, QPlainTextEdit { border: 1px solid #d9d9d9; border-radius: 4px; background: #fafbfc; color: #1a1a2e; font-family: 'Consolas', 'Courier New', monospace; font-size: 12px; }
            QLabel { color: #1a1a2e; }
            QCheckBox { color: #1a1a2e; }
            QCheckBox::indicator { width: 16px; height: 16px; }
            QListWidget { border: 1px solid #d9d9d9; border-radius: 4px; background: white; color: #1a1a2e; }
            QListWidget::item { padding: 5px; }
            QSplitter::handle { background: #d0d5dd; width: 2px; }
        """)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.mode_tabs = QTabWidget()
        main_layout.addWidget(self.mode_tabs)

        # ---- File Mode Tab ----
        self.file_mode_widget = QWidget()
        file_layout = QVBoxLayout(self.file_mode_widget)

        folder_group = QGroupBox('📁 Folder Selection')
        folder_grid = QGridLayout()
        folder_grid.addWidget(QLabel('Baseline Folder:'), 0, 0)
        self.f1_edit = QLineEdit()
        folder_grid.addWidget(self.f1_edit, 0, 1)
        b1 = QPushButton('Browse')
        b1.clicked.connect(lambda: self._browse(self.f1_edit))
        folder_grid.addWidget(b1, 0, 2)
        folder_grid.addWidget(QLabel('Comparison Folder:'), 1, 0)
        self.f2_edit = QLineEdit()
        folder_grid.addWidget(self.f2_edit, 1, 1)
        b2 = QPushButton('Browse')
        b2.clicked.connect(lambda: self._browse(self.f2_edit))
        folder_grid.addWidget(b2, 1, 2)
        folder_group.setLayout(folder_grid)
        file_layout.addWidget(folder_group)

        col_group = QGroupBox('⚙ Column Configuration')
        col_grid = QGridLayout()
        col_grid.addWidget(QLabel('SKU Column:'), 0, 0)
        self.sku_edit = QLineEdit()
        col_grid.addWidget(self.sku_edit, 0, 1)
        self.auto_detect_check = QCheckBox('Auto-detect SKU column')
        self.auto_detect_check.setChecked(True)
        col_grid.addWidget(self.auto_detect_check, 0, 2, 1, 2)
        col_grid.addWidget(QLabel('Priority List:'), 1, 0, Qt.AlignTop)
        self.priority_edit = QPlainTextEdit()
        self.priority_edit.setMaximumHeight(100)
        col_grid.addWidget(self.priority_edit, 1, 1, 1, 3)
        col_grid.addWidget(QLabel('Compare Columns:'), 2, 0)
        self.cols_edit = QLineEdit()
        self.cols_edit.setPlaceholderText('e.g. price, stock (empty=all except excluded)')
        col_grid.addWidget(self.cols_edit, 2, 1, 1, 2)
        self.select_cols_btn = QPushButton('📋 Select from File')
        self.select_cols_btn.clicked.connect(self._select_columns_from_file)
        col_grid.addWidget(self.select_cols_btn, 2, 3)
        self.normalize_check = QCheckBox('Normalize SKUs')
        self.normalize_check.setChecked(True)
        col_grid.addWidget(self.normalize_check, 3, 0)
        self.case_check = QCheckBox('Case Sensitive')
        self.case_check.setChecked(True)
        col_grid.addWidget(self.case_check, 3, 1)
        self.debug_check = QCheckBox('Debug Logging')
        self.debug_check.setChecked(True)
        col_grid.addWidget(self.debug_check, 3, 2)
        col_grid.addWidget(QLabel('Workers:'), 4, 0)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 16)
        self.workers_spin.setValue(4)
        col_grid.addWidget(self.workers_spin, 4, 1)
        col_grid.addWidget(QLabel('File Filter:'), 4, 2)
        self.filter_combo = QComboBox()
        self.filter_combo.addItems(['All supported', '*.csv', '*.xlsx', '*.xml', '*.zip'])
        col_grid.addWidget(self.filter_combo, 4, 3)
        col_group.setLayout(col_grid)
        file_layout.addWidget(col_group)

        btn_layout = QHBoxLayout()
        self.run_btn = QPushButton('▶ Start Validation')
        self.run_btn.clicked.connect(self.start_validation_file)
        btn_layout.addWidget(self.run_btn)
        self.export_btn = QPushButton('📊 Export Report')
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_report)
        btn_layout.addWidget(self.export_btn)
        self.clear_btn = QPushButton('🗑 Clear')
        self.clear_btn.clicked.connect(self.clear_results)
        btn_layout.addWidget(self.clear_btn)
        btn_layout.addStretch()
        file_layout.addLayout(btn_layout)

        self.mode_tabs.addTab(self.file_mode_widget, "📁 File Mode")

        # ---- API Mode Tab ----
        self.api_mode_widget = QWidget()
        api_layout = QVBoxLayout(self.api_mode_widget)
        api_layout.setSpacing(6)

        api_top = QHBoxLayout()
        api_top.addWidget(QLabel("Project:"))
        self.api_project_combo = QComboBox()
        self.api_project_combo.setMinimumWidth(180)
        api_top.addWidget(self.api_project_combo)
        self.api_fetch_projects_btn = QPushButton("Refresh Projects")
        self.api_fetch_projects_btn.setFixedHeight(28)
        self.api_fetch_projects_btn.setMinimumWidth(130)
        self.api_fetch_projects_btn.clicked.connect(self.api_fetch_projects)
        api_top.addWidget(self.api_fetch_projects_btn)
        api_top.addStretch()
        api_layout.addLayout(api_top)

        sites_widget = QWidget()
        sites_layout = QHBoxLayout(sites_widget)
        sites_layout.setContentsMargins(0,0,0,0)
        sites_layout.setSpacing(6)

        s1_group = QGroupBox("Site 1 (Baseline)")
        s1_layout = QHBoxLayout()
        s1_layout.setSpacing(4)
        s1_layout.addWidget(QLabel("Site:"))
        self.api_site1_combo = QComboBox()
        self.api_site1_combo.setMinimumWidth(140)
        s1_layout.addWidget(self.api_site1_combo)
        self.api_site1_search = QLineEdit()
        self.api_site1_search.setPlaceholderText("🔍 Search...")
        self.api_site1_search.setMaximumWidth(100)
        self.api_site1_search.textChanged.connect(lambda text: self.filter_site_combo(1, text))
        s1_layout.addWidget(self.api_site1_search)
        self.api_fetch_sites1_btn = QPushButton("Fetch")
        self.api_fetch_sites1_btn.setFixedSize(100, 28)
        self.api_fetch_sites1_btn.clicked.connect(lambda: self.api_fetch_sites(1))
        s1_layout.addWidget(self.api_fetch_sites1_btn)
        s1_group.setLayout(s1_layout)
        sites_layout.addWidget(s1_group)

        s2_group = QGroupBox("Site 2 (Comparison)")
        s2_layout = QHBoxLayout()
        s2_layout.setSpacing(4)
        s2_layout.addWidget(QLabel("Site:"))
        self.api_site2_combo = QComboBox()
        self.api_site2_combo.setMinimumWidth(140)
        s2_layout.addWidget(self.api_site2_combo)
        self.api_site2_search = QLineEdit()
        self.api_site2_search.setPlaceholderText("🔍 Search...")
        self.api_site2_search.setMaximumWidth(100)
        self.api_site2_search.textChanged.connect(lambda text: self.filter_site_combo(2, text))
        s2_layout.addWidget(self.api_site2_search)
        self.api_fetch_sites2_btn = QPushButton("Fetch")
        self.api_fetch_sites2_btn.setFixedSize(100, 28)
        self.api_fetch_sites2_btn.clicked.connect(lambda: self.api_fetch_sites(2))
        s2_layout.addWidget(self.api_fetch_sites2_btn)
        s2_group.setLayout(s2_layout)
        sites_layout.addWidget(s2_group)

        api_layout.addWidget(sites_widget)

        channel_group = QGroupBox("Channels to compare (auto-matched by name)")
        channel_layout = QVBoxLayout()
        channel_layout.setSpacing(4)
        self.api_channel_count_label = QLabel("Selected: 0 / 0")
        channel_layout.addWidget(self.api_channel_count_label)
        self.api_channel_list = QListWidget()
        self.api_channel_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.api_channel_list.itemSelectionChanged.connect(self.update_channel_count)
        channel_layout.addWidget(self.api_channel_list)
        
        api_btn_row = QHBoxLayout()
        self.api_fetch_channels_btn = QPushButton("Fetch & Match Channels")
        self.api_fetch_channels_btn.setMinimumHeight(30)
        self.api_fetch_channels_btn.clicked.connect(self.api_fetch_channels)
        api_btn_row.addWidget(self.api_fetch_channels_btn)
        
        self.api_run_btn = QPushButton("▶ Download & Compare")
        self.api_run_btn.setMinimumHeight(30)
        self.api_run_btn.clicked.connect(self.api_download_and_compare)
        api_btn_row.addWidget(self.api_run_btn)
        
        channel_layout.addLayout(api_btn_row)
        channel_group.setLayout(channel_layout)
        api_layout.addWidget(channel_group, stretch=1)

        self.api_log = QTextEdit()
        self.api_log.setReadOnly(True)
        api_layout.addWidget(self.api_log, stretch=1)

        self.mode_tabs.addTab(self.api_mode_widget, "🌐 API Mode")

        # Results panel
        results_widget = QWidget()
        results_layout = QVBoxLayout(results_widget)
        self.tabs = QTabWidget()
        self.summary_text = QTextEdit()
        self.summary_text.setReadOnly(True)
        self.tabs.addTab(self.summary_text, '📋 Summary')
        self.column_rate_table = QTableWidget()
        self.column_rate_table.setColumnCount(8)
        self.column_rate_table.setHorizontalHeaderLabels(['Column', 'Status', 'Match Rate', 'Matches', 'Mismatches', 'Total', 'Both Empty', 'One-side Empty'])
        self.column_rate_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tabs.addTab(self.column_rate_table, '📊 Column Match Rates')
        self.log_text = QPlainTextEdit()
        self.log_text.setReadOnly(True)
        self.tabs.addTab(self.log_text, '🐛 Debug Log')
        self.sku_log_text = QPlainTextEdit()
        self.sku_log_text.setReadOnly(True)
        self.tabs.addTab(self.sku_log_text, '🔍 SKU Detection')
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(10)
        self.file_table.setHorizontalHeaderLabels(['File', 'Type', 'Status', 'SKU Col', 'Rows F1', 'Rows F2', 'Common SKUs', 'Diffs', 'Miss F2', 'Miss F1'])
        self.file_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tabs.addTab(self.file_table, '📁 Files')
        self.diff_table = QTableWidget()
        self.diff_table.setColumnCount(5)
        self.diff_table.setHorizontalHeaderLabels(['SKU', 'Column', 'Value F1', 'Value F2', 'File'])
        self.diff_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.tabs.addTab(self.diff_table, '🔍 Differences')
        results_layout.addWidget(self.tabs)
        main_layout.addWidget(results_widget, stretch=2)

        self.progress_bar = QProgressBar()
        main_layout.addWidget(self.progress_bar)
        self.status_label = QLabel('Ready')
        main_layout.addWidget(self.status_label)

    def update_channel_count(self):
        total = self.api_channel_list.count()
        selected = len(self.api_channel_list.selectedItems())
        self.api_channel_count_label.setText(f"Selected: {selected} / {total}")

    def filter_site_combo(self, site_num, text):
        if site_num == 1:
            all_sites = self.api_sites1_all
            combo = self.api_site1_combo
        else:
            all_sites = self.api_sites2_all
            combo = self.api_site2_combo
        text = text.strip().lower()
        combo.clear()
        for s in all_sites:
            if text in s['name'].lower() or text in str(s['id']):
                combo.addItem(f"{s['name']} (ID: {s['id']})", s['id'])

    def _browse(self, edit):
        folder = QFileDialog.getExistingDirectory(self, 'Select Folder')
        if folder:
            edit.setText(folder)

    def _select_columns_from_file(self):
        start_dir = self.f2_edit.text().strip() or self.f1_edit.text().strip() or os.path.expanduser('~')
        file_path, _ = QFileDialog.getOpenFileName(self, 'Select a sample file', start_dir,
            'All Supported (*.csv *.xlsx *.xls *.xml *.zip);;CSV (*.csv);;Excel (*.xlsx *.xls);;XML (*.xml);;ZIP (*.zip)')
        if not file_path:
            return
        current_text = self.cols_edit.text().strip()
        currently_selected = [c.strip() for c in current_text.split(',') if c.strip()] if current_text else []
        dialog = ColumnSelectorDialog(self, file_path, currently_selected)
        if dialog.exec() == QDialog.Accepted:
            selected = dialog.get_selected_columns()
            self.selected_columns = selected
            self.cols_edit.setText(', '.join(selected))

    def start_validation_file(self):
        f1 = self.f1_edit.text().strip()
        f2 = self.f2_edit.text().strip()
        sku = self.sku_edit.text().strip() or None
        cols_text = self.cols_edit.text().strip()
        if not f1 or not f2:
            show_message(self, 'Input Error', 'Please select both folders.', QMessageBox.Warning)
            return
        if cols_text:
            compare_cols = [c.strip() for c in cols_text.split(',') if c.strip()]
        else:
            compare_cols = []
        filt = self.filter_combo.currentText()
        if filt == 'All supported':
            filt = None
        priority_text = self.priority_edit.toPlainText().strip()
        priority_list = [line.strip() for line in priority_text.split('\n') if line.strip()] if priority_text else None
        self.run_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.log_text.clear()
        self.sku_log_text.clear()
        self.api_channel_name_map = {}
        self.api_site1_name = ""
        self.api_site2_name = ""
        self._diff_csv_paths = []
        self.worker = ValidationWorker(f1, f2, sku, compare_cols, filt,
                                       self.workers_spin.value(),
                                       self.normalize_check.isChecked(),
                                       self.case_check.isChecked(),
                                       self.debug_check.isChecked(),
                                       self.auto_detect_check.isChecked(),
                                       priority_list,
                                       self.EXCLUDE_COLUMNS_KEYWORDS)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.error.connect(self._on_error)
        self.worker.start()

    def api_fetch_projects(self):
        self.api_fetch_projects_btn.setEnabled(False)
        self.worker_api = FetchProjectsWorker()
        self.worker_api.log.connect(self.api_log.append)
        self.worker_api.finished.connect(self._api_on_projects)
        self.worker_api.error.connect(lambda e: self.api_log.append(f"Error: {e}"))
        self.worker_api.start()

    def _api_on_projects(self, projects):
        self.api_projects = projects
        self.api_project_combo.clear()
        for p in projects:
            self.api_project_combo.addItem(f"{p['name']} (ID: {p['id']})", p['id'])
        self.api_fetch_projects_btn.setEnabled(True)

    def api_fetch_sites(self, site_num):
        pid = self.api_project_combo.currentData()
        if not pid:
            QMessageBox.warning(self, "Warning", "Select a project first.")
            return
        if pid in self._site_cache:
            self.api_log.append(f"Using cached sites for project {pid}")
            if site_num == 1:
                self._api_on_sites(self._site_cache[pid], 1)
            else:
                self._api_on_sites(self._site_cache[pid], 2)
            return
        self.worker_api = FetchSitesWorker(pid, only_active=False)
        self.worker_api.log.connect(self.api_log.append)
        self.worker_api.finished.connect(lambda sites, num=site_num: self._api_on_sites(sites, num))
        self.worker_api.error.connect(lambda e: self.api_log.append(f"Error: {e}"))
        if site_num == 1:
            self.api_fetch_sites1_btn.setEnabled(False)
        else:
            self.api_fetch_sites2_btn.setEnabled(False)
        self.worker_api.start()

    def _api_on_sites(self, sites, site_num):
        pid = self.api_project_combo.currentData()
        if pid:
            self._site_cache[pid] = sites
        if site_num == 1:
            self.api_sites1_all = sites
            self.api_site1_combo.clear()
            self.api_site1_search.clear()
            for s in sites:
                self.api_site1_combo.addItem(f"{s['name']} (ID: {s['id']})", s['id'])
            self.api_fetch_sites1_btn.setEnabled(True)
        else:
            self.api_sites2_all = sites
            self.api_site2_combo.clear()
            self.api_site2_search.clear()
            for s in sites:
                self.api_site2_combo.addItem(f"{s['name']} (ID: {s['id']})", s['id'])
            self.api_fetch_sites2_btn.setEnabled(True)

    def api_fetch_channels(self):
        sid1 = self.api_site1_combo.currentData()
        sid2 = self.api_site2_combo.currentData()
        if not sid1 or not sid2:
            QMessageBox.warning(self, "Warning", "Select both sites.")
            return
        self.api_fetch_channels_btn.setEnabled(False)
        self.api_log.append("Fetching channels for both sites...")
        self.channel_data1 = None
        self.channel_data2 = None
        self.worker_ch1 = FetchChannelsWorker(sid1)
        self.worker_ch2 = FetchChannelsWorker(sid2)
        self.worker_ch1.log.connect(self.api_log.append)
        self.worker_ch2.log.connect(self.api_log.append)
        self.worker_ch1.finished.connect(lambda data: self._api_channel_received(data, 1))
        self.worker_ch2.finished.connect(lambda data: self._api_channel_received(data, 2))
        self.worker_ch1.error.connect(lambda e: self.api_log.append(f"Error site1: {e}"))
        self.worker_ch2.error.connect(lambda e: self.api_log.append(f"Error site2: {e}"))
        self.worker_ch1.start()
        self.worker_ch2.start()

    def _api_channel_received(self, data, which):
        if which == 1:
            self.channel_data1 = data
        else:
            self.channel_data2 = data
        if self.channel_data1 is not None and self.channel_data2 is not None:
            self._api_match_channels()

    def _api_match_channels(self):
        ch1_dict = {ch['name'].lower(): ch for ch in self.channel_data1}
        ch2_dict = {ch['name'].lower(): ch for ch in self.channel_data2}
        common = set(ch1_dict.keys()) & set(ch2_dict.keys())
        self.api_channel_list.clear()
        self.api_matched_channels = []
        for name in sorted(common):
            ch1 = ch1_dict[name]
            ch2 = ch2_dict[name]
            link1 = ch1.get('link', '')
            link2 = ch2.get('link', '')
            self.api_matched_channels.append((ch1['name'], link1, link2))
            self.api_channel_list.addItem(f"{ch1['name']}")
        self.update_channel_count()
        self.api_log.append(f"Matched {len(common)} channels.")
        self.api_fetch_channels_btn.setEnabled(True)

    def api_download_and_compare(self):
        selected_items = self.api_channel_list.selectedItems()
        if not selected_items:
            QMessageBox.warning(self, "Warning", "Select at least one channel to compare.")
            return
        selected_names = [it.text() for it in selected_items]
        channels_to_compare = [(name, link1, link2) for name, link1, link2 in self.api_matched_channels if name in selected_names]
        if not channels_to_compare:
            QMessageBox.warning(self, "Warning", "No valid channels selected.")
            return
        self.api_run_btn.setEnabled(False)
        self.export_btn.setEnabled(False)
        self.log_text.clear()
        site1_name = self.api_site1_combo.currentText().split(" (ID:")[0]
        site2_name = self.api_site2_combo.currentText().split(" (ID:")[0]
        self.api_site1_name = site1_name
        self.api_site2_name = site2_name
        self.api_channel_name_map = {}
        for name, link1, link2 in channels_to_compare:
            ext = Path(link1.split('?')[0]).suffix or '.csv'
            fname = f"{name}{ext}"
            self.api_channel_name_map[fname] = name
        cols_text = self.cols_edit.text().strip()
        if cols_text:
            compare_cols = [c.strip() for c in cols_text.split(',') if c.strip()]
        else:
            compare_cols = []
        self.worker_api = DownloadAndCompareWorker(
            site1_name, [(c[0], c[1]) for c in channels_to_compare],
            site2_name, [(c[0], c[2]) for c in channels_to_compare],
            sku_col=self.sku_edit.text().strip() or None,
            compare_cols=compare_cols,
            normalize_sku=self.normalize_check.isChecked(),
            case_sensitive=self.case_check.isChecked(),
            debug=self.debug_check.isChecked(),
            auto_detect=self.auto_detect_check.isChecked(),
            exclude_keywords=self.EXCLUDE_COLUMNS_KEYWORDS
        )
        self.worker_api.log.connect(self.api_log.append)
        self.worker_api.progress.connect(self._on_progress)
        self.worker_api.finished.connect(self._api_download_done)
        self.worker_api.error.connect(lambda e: self.api_log.append(f"Error: {e}"))
        self.worker_api.start()

    def _api_download_done(self, results):
        # Build per-channel column stats with channel name from api_channel_name_map
        per_file_rows = []
        for file_name, result in results['files'].items():
            if result['status'] == 'success' and 'column_stats' in result:
                channel_name = self.api_channel_name_map.get(file_name, file_name)
                for col, stats in result['column_stats'].items():
                    per_file_rows.append({
                        'Column': col,
                        'Channel': channel_name,
                        'Status': stats.get('status', ''),
                        'Match_Rate_%': stats['match_rate'],
                        'Matches': stats['exact_matches'],
                        'Mismatches': stats['mismatches'],
                        'Total': stats['total_compared']
                    })
        results['overall_column_stats'] = per_file_rows
        self._on_finished(results)
        self.api_run_btn.setEnabled(True)

    def _on_progress(self, completed, total, filename):
        pct = int(completed / total * 100) if total > 0 else 0
        self.progress_bar.setValue(pct)
        self.status_label.setText(f'[{completed}/{total}] {filename}')

    def _on_finished(self, results):
        try:
            self.results = results
            self.run_btn.setEnabled(True)
            self.export_btn.setEnabled(True)
            self.api_run_btn.setEnabled(True)
            o = results['overall']
            summary = f"""
╔══════════════════════════════════════════════════════════════════╗
║                     VALIDATION SUMMARY                           ║
╚══════════════════════════════════════════════════════════════════╝

📊 Files: {o['total_pairs']} total | ✅ {o['success']} compared | ⏭️ {o['skipped']} skipped | ❌ {o['failed']} failed

📈 Data (from {o['success']} compared files):
   • Total common SKUs compared: {o['total_skus']:,}
   • Total differences found: {o['total_diffs']:,}
   • Total missing SKUs: {o['total_missing']:,}

📊 Overall Match Rate: {o.get('avg_match_rate', 0)}%

💡 Check "SKU Detection" tab for auto-detected columns
💡 Check "Column Match Rates" tab for per-column details
💡 Supports: CSV (|, TAB, ;, ,), Excel (.xlsx/.xls), XML, ZIP (with CSV/Excel/XML), API
            """
            self.summary_text.setHtml(f"<pre style='color: #1a1a2e;'>{summary}</pre>")
            overall_col_stats = results.get('overall_column_stats', [])
            if isinstance(overall_col_stats, list):
                sorted_cols = sorted(overall_col_stats, key=lambda x: x['Match_Rate_%'])
                self.column_rate_table.setRowCount(len(sorted_cols))
                for i, row in enumerate(sorted_cols):
                    col = row['Column']
                    match_rate = row['Match_Rate_%']
                    matches = row['Matches']
                    mismatches = row['Mismatches']
                    total = row['Total']
                    col_item = QTableWidgetItem(col)
                    col_item.setForeground(QColor('#1a1a2e'))
                    self.column_rate_table.setItem(i, 0, col_item)
                    if match_rate == 100:
                        status_text, bg_color, text_color = '✅ PERFECT', QColor('#52c41a'), QColor('white')
                    elif match_rate >= 95:
                        status_text, bg_color, text_color = '🟢 GOOD', QColor('#73d13d'), QColor('white')
                    elif match_rate >= 80:
                        status_text, bg_color, text_color = '🟡 FAIR', QColor('#faad14'), QColor('#1a1a2e')
                    elif match_rate >= 50:
                        status_text, bg_color, text_color = '🟠 POOR', QColor('#ff7a45'), QColor('white')
                    else:
                        status_text, bg_color, text_color = '🔴 BAD', QColor('#ff4d4f'), QColor('white')
                    status_item = QTableWidgetItem(status_text)
                    status_item.setBackground(bg_color)
                    status_item.setForeground(text_color)
                    status_item.setFont(QFont('', -1, QFont.Bold))
                    self.column_rate_table.setItem(i, 1, status_item)
                    rate_item = QTableWidgetItem(f"{match_rate:.1f}%")
                    rate_item.setForeground(bg_color)
                    rate_item.setFont(QFont('', -1, QFont.Bold))
                    self.column_rate_table.setItem(i, 2, rate_item)
                    self.column_rate_table.setItem(i, 3, QTableWidgetItem(f"{matches:,}"))
                    self.column_rate_table.setItem(i, 4, QTableWidgetItem(f"{mismatches:,}"))
                    self.column_rate_table.setItem(i, 5, QTableWidgetItem(f"{total:,}"))
                    self.column_rate_table.setItem(i, 6, QTableWidgetItem(""))
                    self.column_rate_table.setItem(i, 7, QTableWidgetItem(""))
            else:
                sorted_cols = sorted(overall_col_stats.items(), key=lambda x: x[1]['match_rate'])
                self.column_rate_table.setRowCount(len(sorted_cols))
                for i, (col, stats) in enumerate(sorted_cols):
                    col_item = QTableWidgetItem(col)
                    col_item.setForeground(QColor('#1a1a2e'))
                    self.column_rate_table.setItem(i, 0, col_item)
                    match_rate = stats['match_rate']
                    if match_rate == 100:
                        status_text, bg_color, text_color = '✅ PERFECT', QColor('#52c41a'), QColor('white')
                    elif match_rate >= 95:
                        status_text, bg_color, text_color = '🟢 GOOD', QColor('#73d13d'), QColor('white')
                    elif match_rate >= 80:
                        status_text, bg_color, text_color = '🟡 FAIR', QColor('#faad14'), QColor('#1a1a2e')
                    elif match_rate >= 50:
                        status_text, bg_color, text_color = '🟠 POOR', QColor('#ff7a45'), QColor('white')
                    else:
                        status_text, bg_color, text_color = '🔴 BAD', QColor('#ff4d4f'), QColor('white')
                    status_item = QTableWidgetItem(status_text)
                    status_item.setBackground(bg_color)
                    status_item.setForeground(text_color)
                    status_item.setFont(QFont('', -1, QFont.Bold))
                    self.column_rate_table.setItem(i, 1, status_item)
                    rate_item = QTableWidgetItem(f"{match_rate:.1f}%")
                    rate_item.setForeground(bg_color)
                    rate_item.setFont(QFont('', -1, QFont.Bold))
                    self.column_rate_table.setItem(i, 2, rate_item)
                    self.column_rate_table.setItem(i, 3, QTableWidgetItem(f"{stats['exact_matches']:,}"))
                    self.column_rate_table.setItem(i, 4, QTableWidgetItem(f"{stats['mismatches']:,}"))
                    self.column_rate_table.setItem(i, 5, QTableWidgetItem(f"{stats['total_compared']:,}"))
                    self.column_rate_table.setItem(i, 6, QTableWidgetItem(f"{stats.get('both_nan', 0):,}"))
                    one_side = stats.get('only_file1_nan', 0) + stats.get('only_file2_nan', 0)
                    self.column_rate_table.setItem(i, 7, QTableWidgetItem(f"{one_side:,}"))
            debug_logs = results.get('debug_logs', [])
            log_lines = []
            log_lines.append("=" * 110)
            log_lines.append("DEBUG LOG")
            log_lines.append("=" * 110)
            log_lines.append("")
            for log in debug_logs:
                time_str = log['time']
                category = log['category']
                file_name = log.get('file', '')
                message = log['message']
                if len(file_name) > 38:
                    file_name = file_name[:35] + '...'
                log_lines.append(f"{time_str:<14s} [{category:<6s}] {file_name:<40s} {message}")
            log_lines.append("")
            log_lines.append("=" * 110)
            log_lines.append(f"LOG SUMMARY: {results.get('log_summary', {})}")
            log_lines.append("=" * 110)
            self.log_text.setPlainText('\n'.join(log_lines))
            sku_log = results.get('sku_detection_log', [])
            if sku_log:
                sku_lines = ["=" * 80, "SKU COLUMN DETECTION LOG", "=" * 80, ""] + sku_log
                self.sku_log_text.setPlainText('\n'.join(sku_lines))
            if self.api_channel_name_map:
                site1 = self.api_site1_name or 'Value F1'
                site2 = self.api_site2_name or 'Value F2'
                self.diff_table.setHorizontalHeaderLabels(['SKU', 'Column', site1, site2, 'Channel'])
            else:
                self.diff_table.setHorizontalHeaderLabels(['SKU', 'Column', 'Value F1', 'Value F2', 'File'])
            files = results['files']
            self.file_table.setRowCount(len(files))
            self._diff_csv_paths = []
            all_diffs_preview = []
            for i, (name, r) in enumerate(files.items()):
                display_name = name
                if self.api_channel_name_map and name in self.api_channel_name_map:
                    display_name = self.api_channel_name_map[name]
                self.file_table.setItem(i, 0, QTableWidgetItem(display_name))
                ext = Path(name).suffix.upper()
                self.file_table.setItem(i, 1, QTableWidgetItem(ext))
                if r['status'] == 'success':
                    status_item = QTableWidgetItem('✅ Compared')
                    status_item.setForeground(QColor('#52c41a'))
                    self.file_table.setItem(i, 2, status_item)
                    sku_info = f"{r.get('sku_col1', '')}"
                    if r.get('sku_col1') != r.get('sku_col2'):
                        sku_info += f" / {r.get('sku_col2', '')}"
                    self.file_table.setItem(i, 3, QTableWidgetItem(sku_info))
                    s = r['summary']
                    self.file_table.setItem(i, 4, QTableWidgetItem(str(s['rows_file1'])))
                    self.file_table.setItem(i, 5, QTableWidgetItem(str(s['rows_file2'])))
                    self.file_table.setItem(i, 6, QTableWidgetItem(str(s['common_skus'])))
                    diff_item = QTableWidgetItem(str(s['total_differences']))
                    if s['total_differences'] > 0:
                        diff_item.setForeground(QColor('#cf1322'))
                        diff_item.setFont(QFont('', -1, QFont.Bold))
                    self.file_table.setItem(i, 7, diff_item)
                    self.file_table.setItem(i, 8, QTableWidgetItem(str(s['only_in_file1'])))
                    self.file_table.setItem(i, 9, QTableWidgetItem(str(s['only_in_file2'])))
                    diff_csv = r.get('diff_csv_path')
                    if diff_csv and Path(diff_csv).exists():
                        self._diff_csv_paths.append((display_name, diff_csv))
                        try:
                            with open(diff_csv, 'r', encoding='utf-8-sig') as f:
                                reader = csv.reader(f)
                                next(reader)  # skip header
                                for _ in range(10):
                                    row = next(reader, None)
                                    if row is None:
                                        break
                                    d_copy = {'sku': row[0], 'column': row[1], 'value_file1': row[2], 'value_file2': row[3], 'file': display_name}
                                    all_diffs_preview.append(d_copy)
                        except:
                            pass
                elif r['status'] == 'skipped':
                    status_item = QTableWidgetItem('⏭️ Skipped')
                    status_item.setForeground(QColor('#faad14'))
                    self.file_table.setItem(i, 2, status_item)
                    self.file_table.setItem(i, 3, QTableWidgetItem(r.get('reason', '')))
                else:
                    status_item = QTableWidgetItem('❌ Failed')
                    status_item.setForeground(QColor('#ff4d4f'))
                    self.file_table.setItem(i, 2, status_item)
                    self.file_table.setItem(i, 3, QTableWidgetItem(r.get('error', '')[:50]))
            self.diff_table.setRowCount(min(len(all_diffs_preview), 10))
            for i, d in enumerate(all_diffs_preview[:10]):
                self.diff_table.setItem(i, 0, QTableWidgetItem(str(d['sku'])))
                self.diff_table.setItem(i, 1, QTableWidgetItem(d['column']))
                v1 = QTableWidgetItem(str(d['value_file1']))
                v2 = QTableWidgetItem(str(d['value_file2']))
                if d['value_file1'] != d['value_file2']:
                    v1.setBackground(QColor('#fff1f0'))
                    v2.setBackground(QColor('#f6ffed'))
                self.diff_table.setItem(i, 2, v1)
                self.diff_table.setItem(i, 3, v2)
                self.diff_table.setItem(i, 4, QTableWidgetItem(d['file']))
            self.status_label.setText(f'✅ Done: {o["success"]} compared, {o["skipped"]} skipped, {o["failed"]} failed')
            self.tabs.setCurrentIndex(1)
            self._auto_export_report()
            CompletionDialog(self, 'Validation Complete', {
                'files': o['success'],
                'total_pairs': o['total_pairs'],
                'skipped': o['skipped'],
                'failed': o['failed'],
                'match_rate': o.get('avg_match_rate', 0),
                'diffs': o['total_diffs']
            }).exec()
        except Exception as e:
            self.status_label.setText(f'❌ Error displaying results: {str(e)[:100]}')
            self.export_btn.setEnabled(True)
            show_message(self, 'Display Error',
                         f'Results saved but display failed:\n{str(e)[:200]}\n\nYou can still export the report.',
                         QMessageBox.Warning)

    def _extract_country_code(self, site_name):
        match = re.search(r'\.([A-Z]{2})\b', site_name)
        if match:
            return match.group(1)
        match = re.search(r'\b([A-Z]{2})\b', site_name)
        if match:
            return match.group(1)
        return site_name[:2].upper() if len(site_name) >= 2 else site_name.upper()

    def _auto_export_report(self):
        if not self.AUTO_EXPORT_DIR:
            return
        try:
            export_dir = Path(self.AUTO_EXPORT_DIR)
            export_dir.mkdir(exist_ok=True)
            if self.api_channel_name_map and self.api_site1_name and self.api_site2_name:
                safe_site1 = re.sub(r'[<>:"/\\|?*]', '_', self.api_site1_name)
                safe_site2 = re.sub(r'[<>:"/\\|?*]', '_', self.api_site2_name)
                suffix = f"_{safe_site1}_vs_{safe_site2}"
            else:
                suffix = ""
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            zip_path = export_dir / f"validation{suffix}_{timestamp}.zip"
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                # Summary
                pd.DataFrame([self.results['overall']]).to_csv(tmpdir / 'Summary.csv', index=False, encoding='utf-8-sig')
                # Column Match Rates
                col_stats = self.results.get('overall_column_stats', [])
                if col_stats:
                    if isinstance(col_stats, dict):
                        rows = [{'Column': col, 'Status': s.get('status',''), 'Match_Rate_%': s['match_rate'],
                                'Matches': s['exact_matches'], 'Mismatches': s['mismatches'], 'Total': s['total_compared']}
                                for col, s in col_stats.items()]
                    else:
                        rows = col_stats
                    pd.DataFrame(rows).sort_values('Match_Rate_%').to_csv(tmpdir / 'Column_Match_Rates.csv', index=False, encoding='utf-8-sig')
                # Files
                file_rows = []
                for name, r in self.results['files'].items():
                    if r['status'] == 'success':
                        display_name = name
                        if self.api_channel_name_map and name in self.api_channel_name_map:
                            display_name = self.api_channel_name_map[name]
                        file_rows.append({'File': display_name, 'SKU_Col_F1': r.get('sku_col1',''), 'SKU_Col_F2': r.get('sku_col2',''), **r['summary']})
                if file_rows:
                    pd.DataFrame(file_rows).to_csv(tmpdir / 'Files.csv', index=False, encoding='utf-8-sig')
                # Debug Log
                if self.results.get('debug_logs'):
                    pd.DataFrame(self.results['debug_logs']).to_csv(tmpdir / 'Debug_Log.csv', index=False, encoding='utf-8-sig')
                # API Log
                api_log_text = self.api_log.toPlainText().strip()
                if api_log_text:
                    api_log_lines = [line for line in api_log_text.split('\n') if line.strip()]
                    if api_log_lines:
                        pd.DataFrame({'Log': api_log_lines}).to_csv(tmpdir / 'API_Log.csv', index=False, encoding='utf-8-sig')
                # Write diff CSVs
                if self.MERGE_DIFF_FILES:
                    diff_dfs = []
                    for display_name, diff_csv in self._diff_csv_paths:
                        if Path(diff_csv).exists():
                            df = pd.read_csv(diff_csv)
                            df['file'] = display_name
                            diff_dfs.append(df)
                    if diff_dfs:
                        combined = pd.concat(diff_dfs, ignore_index=True)
                        if self.api_channel_name_map:
                            site1 = self.api_site1_name or 'Value_F1'
                            site2 = self.api_site2_name or 'Value_F2'
                            combined.rename(columns={'value_file1': site1, 'value_file2': site2, 'file': 'Channel'}, inplace=True)
                        else:
                            combined.rename(columns={'value_file1': 'Value_F1', 'value_file2': 'Value_F2', 'file': 'File'}, inplace=True)
                        combined.to_csv(tmpdir / 'Differences.csv', index=False, encoding='utf-8-sig')
                else:
                    for display_name, diff_csv in self._diff_csv_paths:
                        if Path(diff_csv).exists():
                            df = pd.read_csv(diff_csv)
                            if self.api_channel_name_map:
                                site1 = self.api_site1_name or 'Value_F1'
                                site2 = self.api_site2_name or 'Value_F2'
                                df.rename(columns={'value_file1': site1, 'value_file2': site2}, inplace=True)
                            else:
                                df.rename(columns={'value_file1': 'Value_F1', 'value_file2': 'Value_F2'}, inplace=True)
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', display_name)
                            df.to_csv(tmpdir / f'Differences_{safe_name}.csv', index=False, encoding='utf-8-sig')
                # Create ZIP
                with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for csv_file in sorted(tmpdir.glob('*.csv')):
                        zf.write(csv_file, csv_file.name)
            self.status_label.setText(f'✅ Done + Auto-exported: {zip_path.name}')
        except Exception as e:
            self.status_label.setText(f'✅ Done (auto-export failed: {str(e)[:50]})')

    def _on_error(self, msg):
        self.run_btn.setEnabled(True)
        self.api_run_btn.setEnabled(True)
        show_message(self, 'Error', f'Validation error:\n{msg}', QMessageBox.Critical)

    def export_report(self):
        if not self.results:
            show_message(self, 'No Data', 'No results to export.', QMessageBox.Warning)
            return
        if self.api_channel_name_map and self.api_site1_name and self.api_site2_name:
            safe_site1 = re.sub(r'[<>:"/\\|?*]', '_', self.api_site1_name)
            safe_site2 = re.sub(r'[<>:"/\\|?*]', '_', self.api_site2_name)
            default_name = f'validation_{safe_site1}_vs_{safe_site2}_{datetime.now():%Y%m%d_%H%M%S}.zip'
        else:
            default_name = f'validation_{datetime.now():%Y%m%d_%H%M%S}.zip'
        path = QFileDialog.getSaveFileName(self, 'Save Report', default_name, 'ZIP Archive (*.zip)')[0]
        if not path:
            return
        try:
            self.status_label.setText('Exporting report...')
            QApplication.processEvents()
            with tempfile.TemporaryDirectory() as tmpdir:
                tmpdir = Path(tmpdir)
                # Summary
                pd.DataFrame([self.results['overall']]).to_csv(tmpdir / 'Summary.csv', index=False, encoding='utf-8-sig')
                # Column Match Rates
                col_stats = self.results.get('overall_column_stats', [])
                if col_stats:
                    if isinstance(col_stats, dict):
                        rows = [{'Column': col, 'Status': s.get('status',''), 'Match_Rate_%': s['match_rate'],
                                'Matches': s['exact_matches'], 'Mismatches': s['mismatches'], 'Total': s['total_compared']}
                                for col, s in col_stats.items()]
                    else:
                        rows = col_stats
                    pd.DataFrame(rows).sort_values('Match_Rate_%').to_csv(tmpdir / 'Column_Match_Rates.csv', index=False, encoding='utf-8-sig')
                # Files
                file_rows = []
                for name, r in self.results['files'].items():
                    if r['status'] == 'success':
                        display_name = name
                        if self.api_channel_name_map and name in self.api_channel_name_map:
                            display_name = self.api_channel_name_map[name]
                        file_rows.append({'File': display_name, 'SKU_Col_F1': r.get('sku_col1',''), 'SKU_Col_F2': r.get('sku_col2',''), **r['summary']})
                if file_rows:
                    pd.DataFrame(file_rows).to_csv(tmpdir / 'Files.csv', index=False, encoding='utf-8-sig')
                # Debug Log
                if self.results.get('debug_logs'):
                    pd.DataFrame(self.results['debug_logs']).to_csv(tmpdir / 'Debug_Log.csv', index=False, encoding='utf-8-sig')
                # API Log
                api_log_text = self.api_log.toPlainText().strip()
                if api_log_text:
                    api_log_lines = [line for line in api_log_text.split('\n') if line.strip()]
                    if api_log_lines:
                        pd.DataFrame({'Log': api_log_lines}).to_csv(tmpdir / 'API_Log.csv', index=False, encoding='utf-8-sig')
                # Write diff CSVs
                if self.MERGE_DIFF_FILES:
                    diff_dfs = []
                    for display_name, diff_csv in self._diff_csv_paths:
                        if Path(diff_csv).exists():
                            df = pd.read_csv(diff_csv)
                            df['file'] = display_name
                            diff_dfs.append(df)
                    if diff_dfs:
                        combined = pd.concat(diff_dfs, ignore_index=True)
                        if self.api_channel_name_map:
                            site1 = self.api_site1_name or 'Value_F1'
                            site2 = self.api_site2_name or 'Value_F2'
                            combined.rename(columns={'value_file1': site1, 'value_file2': site2, 'file': 'Channel'}, inplace=True)
                        else:
                            combined.rename(columns={'value_file1': 'Value_F1', 'value_file2': 'Value_F2', 'file': 'File'}, inplace=True)
                        combined.to_csv(tmpdir / 'Differences.csv', index=False, encoding='utf-8-sig')
                else:
                    for display_name, diff_csv in self._diff_csv_paths:
                        if Path(diff_csv).exists():
                            df = pd.read_csv(diff_csv)
                            if self.api_channel_name_map:
                                site1 = self.api_site1_name or 'Value_F1'
                                site2 = self.api_site2_name or 'Value_F2'
                                df.rename(columns={'value_file1': site1, 'value_file2': site2}, inplace=True)
                            else:
                                df.rename(columns={'value_file1': 'Value_F1', 'value_file2': 'Value_F2'}, inplace=True)
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', display_name)
                            df.to_csv(tmpdir / f'Differences_{safe_name}.csv', index=False, encoding='utf-8-sig')
                with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as zf:
                    for csv_file in sorted(tmpdir.glob('*.csv')):
                        zf.write(csv_file, csv_file.name)
            self.status_label.setText('Ready')
            show_message(self, 'Success', f'Report saved to:\n{path}', QMessageBox.Information)
        except Exception as e:
            self.status_label.setText('Ready')
            show_message(self, 'Export Error', str(e), QMessageBox.Critical)

    def clear_results(self):
        self.results = None
        if hasattr(self, '_diff_csv_paths'):
            for _, diff_csv in self._diff_csv_paths:
                try:
                    Path(diff_csv).unlink(missing_ok=True)
                except:
                    pass
        self._diff_csv_paths = []
        self.summary_text.clear()
        self.log_text.clear()
        self.sku_log_text.clear()
        self.column_rate_table.setRowCount(0)
        self.file_table.setRowCount(0)
        self.diff_table.setRowCount(0)
        self.progress_bar.setValue(0)
        self.export_btn.setEnabled(False)
        self.status_label.setText('Ready')


class ValidationWorker(QThread):
    progress = Signal(int, int, str)
    finished = Signal(dict)
    error = Signal(str)
    def __init__(self, folder1, folder2, sku_col, compare_cols, file_pattern, max_workers,
                 normalize_sku, case_sensitive, debug, auto_detect, priority_list,
                 exclude_keywords=None):
        super().__init__()
        self.folder1 = folder1
        self.folder2 = folder2
        self.sku_col = sku_col
        self.compare_cols = compare_cols
        self.file_pattern = file_pattern
        self.max_workers = max_workers
        self.normalize_sku = normalize_sku
        self.case_sensitive = case_sensitive
        self.debug = debug
        self.auto_detect = auto_detect
        self.priority_list = priority_list
        self.exclude_keywords = exclude_keywords

    def run(self):
        try:
            validator = FastBatchValidator(
                self.folder1, self.folder2, self.sku_col, self.compare_cols,
                self.file_pattern, self.max_workers,
                self.normalize_sku, self.case_sensitive,
                self.debug, self.auto_detect, self.priority_list,
                exclude_keywords=self.exclude_keywords
            )
            results = validator.validate_all_parallel(
                progress_callback=lambda c, t, f: self.progress.emit(c, t, f)
            )
            self.finished.emit(results)
        except Exception as e:
            self.error.emit(str(e))


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('Feed Validator')
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor('#f0f2f5'))
    palette.setColor(QPalette.WindowText, QColor('#1a1a2e'))
    palette.setColor(QPalette.Base, QColor('white'))
    palette.setColor(QPalette.Text, QColor('#1a1a2e'))
    palette.setColor(QPalette.Button, QColor('#4096ff'))
    palette.setColor(QPalette.ButtonText, QColor('white'))
    app.setPalette(palette)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
