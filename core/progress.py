"""Progress reporting primitives shared by the core layer and the UI.

Two kinds of updates exist, deliberately kept separate:

  ``ProgressUpdate``  global, monotonic, drives the progress bar and the
                      status line. Throttled: at most one per
                      ``PROGRESS_INTERVAL`` seconds.

  ``PairStatus``      per-pair state, drives one row of the pairing table.
                      Intermediate ("downloading") updates are throttled;
                      state transitions (pending -> done/failed) are not,
                      because the user must see a failure immediately.

Phase weighting
---------------
The bar must never jump backwards. Each phase therefore reports a ``base``
and a ``span`` inside the 0..1 range, and ``overall`` composes them:

    download   base=0.0  span=0.6
    validate   base=0.6  span=0.4

File mode has no download phase, so it reports the validate phase as
base=0.0 span=1.0 and the bar behaves normally.
"""

import threading
import time
from dataclasses import dataclass

PHASE_DOWNLOAD = "download"
PHASE_VALIDATE = "validate"

# Split of the progress bar between the two phases of a download-and-compare
# run. Download dominates because it is usually the slow part.
DOWNLOAD_SPAN = 0.6
VALIDATE_SPAN = 0.4

# Minimum time between two throttled emissions. Without this, eight concurrent
# downloads reading 256 KB chunks would emit thousands of cross-thread signals
# per second and lock up the UI thread.
PROGRESS_INTERVAL = 0.1

PAIR_PENDING = "pending"
PAIR_DOWNLOADING = "downloading"
PAIR_DONE = "done"
PAIR_FAILED = "failed"
PAIR_CANCELLED = "cancelled"


@dataclass(frozen=True)
class ProgressUpdate:
    """One snapshot of overall run progress."""

    phase: str
    index: int
    total: int
    label: str
    fraction: float = 0.0
    detail: str = ""
    base: float = 0.0
    span: float = 1.0

    @property
    def overall(self) -> float:
        """Position on the 0..1 bar, composed from the phase window."""
        frac = (
            0.0
            if self.fraction < 0.0
            else (1.0 if self.fraction > 1.0 else self.fraction)
        )
        value = self.base + self.span * frac
        return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


@dataclass(frozen=True)
class PairStatus:
    """State of a single pair, addressed by its index in the pair list.

    Index (not pair_id) is the key because the downloader rewrites pair_id
    while numbering the run, which would break id-based row lookup whenever a
    row had been deleted in the UI before starting.
    """

    index: int
    state: str
    fraction: float = 0.0
    detail: str = ""
    error: str = ""

    @property
    def percent(self) -> int:
        value = int(self.fraction * 100)
        return 0 if value < 0 else (100 if value > 100 else value)


class Throttle:
    """Allow at most one emission per ``interval`` seconds.

    ``allow(force=True)`` always passes and resets the window, so a final
    100% update is never swallowed by the throttle.
    """

    def __init__(self, interval: float = PROGRESS_INTERVAL):
        self.interval = interval
        self._last = 0.0
        self._lock = threading.Lock()

    def allow(self, force: bool = False) -> bool:
        if force:
            with self._lock:
                self._last = time.monotonic()
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._last >= self.interval:
                self._last = now
                return True
            return False


def format_bytes(value) -> str:
    """Human-readable byte count. Returns '' for an unknown size."""
    if value is None or value <= 0:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""


def truncate(text: str, limit: int = 60) -> str:
    """Shorten a label for a single status line, keeping the tail readable."""
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
