"""Thread-safe, bounded debug logger.

Memory contract: entries are stored in a ``deque`` with ``max_entries`` as a
hard cap, so a long run over thousands of feed files cannot grow the log
without bound. When the cap is hit, the oldest entries are dropped and
``dropped`` is incremented so the UI can tell the user the log was truncated.

Filtering contract: ``enabled`` only gates the verbose ``DEBUG`` category.
``WARN`` and ``ERROR`` are always recorded — turning off debug logging must
never hide a failure from the user.
"""

import threading
from collections import deque
from datetime import datetime

# Categories that are always recorded, even when verbose logging is disabled.
ALWAYS_ON_CATEGORIES = frozenset({"WARN", "ERROR"})

DEFAULT_MAX_ENTRIES = 100_000


class DebugLogger:
    def __init__(self, max_entries: int = DEFAULT_MAX_ENTRIES):
        self.max_entries = max(1, int(max_entries))
        self.logs = deque(maxlen=self.max_entries)
        self.lock = threading.Lock()
        self.enabled = True
        self.dropped = 0

    def log(self, category: str, message: str, filename: str = ""):
        if not self.enabled and category not in ALWAYS_ON_CATEGORIES:
            return
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        with self.lock:
            if len(self.logs) == self.max_entries:
                self.dropped += 1
            self.logs.append(
                {
                    "time": timestamp,
                    "category": category,
                    "file": filename,
                    "message": message,
                }
            )

    def info(self, msg, filename=""):
        self.log("INFO", msg, filename)

    def warn(self, msg, filename=""):
        self.log("WARN", msg, filename)

    def error(self, msg, filename=""):
        self.log("ERROR", msg, filename)

    def match(self, msg, filename=""):
        self.log("MATCH", msg, filename)

    def miss(self, msg, filename=""):
        self.log("MISS", msg, filename)

    def debug(self, msg, filename=""):
        self.log("DEBUG", msg, filename)

    def success(self, msg, filename=""):
        self.log("SUCCESS", msg, filename)

    def stat(self, msg, filename=""):
        self.log("STAT", msg, filename)

    def detect(self, msg, filename=""):
        self.log("DETECT", msg, filename)

    def skip(self, msg, filename=""):
        self.log("SKIP", msg, filename)

    def get_logs(self, categories=None):
        with self.lock:
            if categories:
                return [l for l in self.logs if l["category"] in categories]
            return list(self.logs)

    def clear(self):
        with self.lock:
            self.logs.clear()
            self.dropped = 0

    def get_summary(self):
        with self.lock:
            summary = {}
            for log in self.logs:
                cat = log["category"]
                summary[cat] = summary.get(cat, 0) + 1
            if self.dropped:
                summary["DROPPED"] = self.dropped
            return summary
