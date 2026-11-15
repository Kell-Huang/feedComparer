"""Intelligent SKU column detector (priority list + edit distance).

Thread-safe: detection_log is protected by an internal lock so concurrent
calls from FastBatchValidator's worker pool produce consistent logs.
"""

import re
import threading
from collections import deque
from typing import Dict, List, Optional, Tuple

# Hard cap on the detection log so a run over thousands of files cannot grow
# it without bound. The oldest entries are dropped first.
MAX_LOG_ENTRIES = 20_000


class SKUColumnDetector:
    # Priority order for target keywords (ean > at_sku > sku > id).
    TARGET_KEYWORDS = ["ean", "at_sku", "sku", "id"]

    # Minimum score for a column to be accepted as the SKU key. Without a
    # floor, the priority bonus below could push an unrelated name over the
    # line: "name" scored 0.41 against "ean" and was silently chosen as the
    # join key, which silently produced garbage diffs instead of skipping.
    MIN_ACCEPTABLE_SCORE = 0.45

    # The priority bonus only applies when the string is genuinely similar;
    # awarding it to weak matches is what produced the false positive above.
    BONUS_MIN_SIMILARITY = 0.6

    SKU_PATTERNS = [
        {"pattern": r"^sku$", "weight": 1.0, "label": "SKU"},
        {"pattern": r"^id$", "weight": 0.95, "label": "ID"},
        {"pattern": r"^ean$", "weight": 0.9, "label": "EAN"},
        {"pattern": r"^gtin$", "weight": 0.9, "label": "GTIN"},
        {"pattern": r"^upc$", "weight": 0.9, "label": "UPC"},
        {"pattern": r"^mpn$", "weight": 0.9, "label": "MPN"},
        {"pattern": r"^offerid$", "weight": 0.95, "label": "OfferID"},
        {"pattern": r"^productid$", "weight": 0.95, "label": "ProductID"},
        {"pattern": r"^itemid$", "weight": 0.9, "label": "ItemID"},
        {"pattern": r"^articleid$", "weight": 0.9, "label": "ArticleID"},
        {
            "pattern": r"^sku[_\s]?(id|code|num|no|number)?$",
            "weight": 0.95,
            "label": "SKU variant",
        },
        {
            "pattern": r"^(product|item|article|offer)[_\s]?(sku|id|code)$",
            "weight": 0.9,
            "label": "Product/Item ID",
        },
        {
            "pattern": r"^(product|item|article|offer)[_\s]?(number|num|no)$",
            "weight": 0.85,
            "label": "Product Number",
        },
        {
            "pattern": r"^.*(sku|stock[_\s]?keeping[_\s]?unit).*$",
            "weight": 0.8,
            "label": "Contains SKU",
        },
        {
            "pattern": r"^ean[_\s]?(code|number|num|no)?$",
            "weight": 0.9,
            "label": "EAN variant",
        },
        {
            "pattern": r"^gtin[_\s]?(code|number|num|no)?$",
            "weight": 0.9,
            "label": "GTIN variant",
        },
        {
            "pattern": r"^upc[_\s]?(code|number|num|no)?$",
            "weight": 0.9,
            "label": "UPC variant",
        },
        {
            "pattern": r"^mpn[_\s]?(code|number|num|no)?$",
            "weight": 0.85,
            "label": "MPN variant",
        },
        {
            "pattern": r"^model[_\s]?(number|num|no|id|code)?$",
            "weight": 0.7,
            "label": "Model Number",
        },
        {
            "pattern": r"^part[_\s]?(number|num|no|id|code)?$",
            "weight": 0.7,
            "label": "Part Number",
        },
    ]

    EXCLUDE_PATTERNS = [
        r"^category[_\s]?id$",
        r"^parent[_\s]?id$",
        r"^group[_\s]?id$",
        r"^manufacturer[_\s]?id$",
        r"^brand[_\s]?id$",
        r"^country[_\s]?id$",
        r"^tax[_\s]?id$",
        r"^currency[_\s]?id$",
        r"^language[_\s]?id$",
        r"^warehouse[_\s]?id$",
        r"^store[_\s]?id$",
        r"^shop[_\s]?id$",
        r"^row[_\s]?id$",
        r"^index$",
        r"^unnamed",
    ]

    def __init__(
        self,
        user_specified: Optional[str] = None,
        priority_list: Optional[List[str]] = None,
    ):
        self.user_specified = user_specified
        self.priority_list = priority_list or []
        self.detection_log = deque(maxlen=MAX_LOG_ENTRIES)
        self._log_lock = threading.Lock()
        # Compiling once avoids re-parsing the same patterns per column.
        self._exclude_res = [
            re.compile(p, re.IGNORECASE) for p in self.EXCLUDE_PATTERNS
        ]
        self._sku_res = [
            (re.compile(p["pattern"], re.IGNORECASE), p["weight"], p["label"])
            for p in self.SKU_PATTERNS
        ]

    def _append_log(self, msg: str):
        with self._log_lock:
            self.detection_log.append(msg)

    def normalize_col(self, col_name: str) -> str:
        n = str(col_name).strip().lower()
        n = n.strip('"').strip("'")
        # Handle namespace prefixes: g:id -> id, c:price -> price.
        if ":" in n:
            parts = n.split(":")
            if len(parts[0]) <= 3:
                n = parts[-1]
        n = re.sub(r"[\s\-_]+", "_", n)
        n = re.sub(r"[^a-z0-9_]", "", n)
        n = n.strip("_")
        return n

    def is_excluded(self, col_name: str) -> bool:
        normalized = self.normalize_col(col_name)
        return any(r.match(normalized) for r in self._exclude_res)

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
                curr_row.append(
                    min(curr_row[j] + 1, prev_row[j + 1] + 1, prev_row[j] + cost)
                )
            prev_row = curr_row
        return prev_row[-1]

    def score_column(self, col_name: str) -> Tuple[float, str]:
        """Score a column name using priority list, patterns, and edit distance."""
        # 1. User specified exact match
        if self.user_specified:
            if col_name == self.user_specified:
                return 1.0, "User specified (exact)"
            if col_name.lower() == self.user_specified.lower():
                return 0.98, "User specified (case-insensitive)"
            if self.normalize_col(col_name) == self.normalize_col(self.user_specified):
                return 0.96, "User specified (normalized)"

        # 2. Priority list
        if self.priority_list:
            for idx, priority_col in enumerate(self.priority_list):
                priority_col = priority_col.strip()
                if not priority_col:
                    continue
                if col_name == priority_col:
                    return max(0.95 - (idx * 0.02), 0.8), f"Priority #{idx + 1} (exact)"
                if col_name.lower() == priority_col.lower():
                    return (
                        max(0.93 - (idx * 0.02), 0.78),
                        f"Priority #{idx + 1} (case-insensitive)",
                    )
                if self.normalize_col(col_name) == self.normalize_col(priority_col):
                    return (
                        max(0.91 - (idx * 0.02), 0.76),
                        f"Priority #{idx + 1} (normalized)",
                    )

        # 3. Exclude patterns
        if self.is_excluded(col_name):
            return 0.0, "Excluded"

        # 4. Exact pattern match (high weight)
        normalized = self.normalize_col(col_name)
        for compiled, weight, label in self._sku_res:
            if compiled.match(normalized):
                return weight, label

        # 5. Edit distance based scoring with priority keywords
        normalized_lower = normalized.lower()
        best_score = 0.0
        best_keyword = ""
        for kw in self.TARGET_KEYWORDS:
            dist = self._levenshtein_distance(normalized_lower, kw)
            max_len = max(len(normalized_lower), len(kw))
            if max_len == 0:
                continue
            similarity = 1 - (dist / max_len)
            length_penalty = min(len(normalized_lower) / 20, 0.3)
            score = (similarity - length_penalty) * 0.7
            score = max(0, min(0.7, score))
            if similarity >= self.BONUS_MIN_SIMILARITY:
                priority_bonus = (
                    len(self.TARGET_KEYWORDS) - self.TARGET_KEYWORDS.index(kw)
                ) * 0.05
                score += priority_bonus
            if score > best_score:
                best_score = score
                best_keyword = kw

        # 6. Contains keyword fallback
        if best_score < 0.4:
            keywords = [
                "sku",
                "ean",
                "gtin",
                "upc",
                "mpn",
                "offerid",
                "productid",
                "itemid",
            ]
            for kw in keywords:
                if kw in normalized_lower:
                    if normalized_lower == kw:
                        return 0.65, f'Exact match "{kw}"'
                    bonus = max(0, (15 - len(normalized_lower)) * 0.003)
                    return min(0.55 + bonus, 0.65), f'Contains "{kw}"'
            return 0.0, "No match"

        if best_score >= 0.4:
            return best_score, f'Similar to "{best_keyword}" (distance)'
        return 0.0, "No match"

    def detect_sku_column(
        self, columns: List[str], file_name: str = ""
    ) -> Tuple[Optional[str], float, str, Dict]:
        candidates = {}
        for col in columns:
            score, label = self.score_column(col)
            if score >= self.MIN_ACCEPTABLE_SCORE:
                candidates[col] = (score, label)
        if not candidates:
            self._append_log(
                f"[{file_name}] ❌ No SKU column detected. Available: {columns[:10]}..."
            )
            return None, 0.0, "Not found", {}
        sorted_candidates = sorted(
            candidates.items(), key=lambda x: (-x[1][0], len(x[0]))
        )
        best_col, (best_score, best_label) = sorted_candidates[0]
        candidate_str = ", ".join(
            [f"'{c}'({s:.2f}:{l})" for c, (s, l) in sorted_candidates[:5]]
        )
        self._append_log(f"[{file_name}] 🔍 SKU candidates: {candidate_str}")
        if best_score >= 0.8:
            self._append_log(
                f"[{file_name}] ✅ Detected: '{best_col}' (score: {best_score:.2f}, {best_label})"
            )
        elif best_score >= 0.5:
            self._append_log(
                f"[{file_name}] ⚠️ Low confidence: '{best_col}' (score: {best_score:.2f}, {best_label})"
            )
        else:
            self._append_log(f"[{file_name}] ❌ No reliable SKU column found")
        return best_col, best_score, best_label, dict(sorted_candidates)

    def detect_common_sku(
        self, df1_columns: List[str], df2_columns: List[str], file_name: str = ""
    ) -> Tuple[Optional[str], Optional[str], float]:
        col1, score1, label1, cands1 = self.detect_sku_column(
            df1_columns, f"{file_name} (File1)"
        )
        col2, score2, label2, cands2 = self.detect_sku_column(
            df2_columns, f"{file_name} (File2)"
        )
        if col1 is None or col2 is None:
            return None, None, 0.0
        confidence = min(score1, score2)
        if col1 != col2:
            self._append_log(
                f"[{file_name}] ⚠️ Different SKU columns: File1='{col1}', File2='{col2}'"
            )
        return col1, col2, confidence

    def get_log(self) -> List[str]:
        """Return a snapshot copy; safe to consume from any thread."""
        with self._log_lock:
            return list(self.detection_log)

    def clear_log(self):
        with self._log_lock:
            self.detection_log.clear()
