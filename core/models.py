"""Data models shared between core and UI layers."""
import shutil
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class PairSource(Enum):
    API = "api"
    MANUAL = "manual"


@dataclass
class DownloadPair:
    """A logical (left=baseline, right=comparison) pair to download and diff.

    Contracts:
      - left/right extension must match after download, otherwise the pair
        cannot be matched by FastBatchValidator (filename-intersection based).
      - left_name/right_name should be non-empty; downloader is responsible
        for fallback naming (e.g. ``url_1``). Empty names degrade gracefully
        in display_name rather than being silently rewritten here.
    """
    pair_id: str
    left_name: str
    left_url: str
    right_name: str
    right_url: str

    @property
    def display_name(self) -> str:
        left = self.left_name or f"<{self.left_url}>"
        right = self.right_name or f"<{self.right_url}>"
        return f"{left} vs {right}"


@dataclass
class DownloadResult:
    """Outcome of downloading one pair.

    Contract:
      - success=True  -> left_path and right_path are non-None, error is empty.
      - success=False -> error is non-empty. left_path / right_path may still
        hold partial results for diagnostics.
      - cancelled=True -> the run was aborted by the user. ``error`` is
        "Cancelled"; callers must not report this as a failure.
    """
    pair: DownloadPair
    success: bool
    error: str = ""
    left_path: Optional[Path] = None
    right_path: Optional[Path] = None
    cancelled: bool = False


@dataclass
class DownloadSession:
    """Aggregated result of downloading all pairs before diffing.

    Contract: ``pairs`` must have unique ``pair_id`` values. Uniqueness is
    enforced by the downloader (which owns id generation), not by this model.
    """
    source: PairSource
    site1_label: str = ""
    site2_label: str = ""
    base_dir: Optional[Path] = None
    pairs: list = field(default_factory=list)            # list[DownloadPair]
    results: list = field(default_factory=list)          # list[DownloadResult]

    @property
    def successful(self) -> list:
        return [r for r in self.results if r.success]

    @property
    def failed(self) -> list:
        """Pairs that genuinely failed. Cancelled pairs are excluded."""
        return [r for r in self.results if not r.success and not r.cancelled]

    @property
    def cancelled(self) -> list:
        return [r for r in self.results if r.cancelled]

    def name_map(self) -> dict:
        """Map pair_id -> display_name. Available before any download."""
        return {p.pair_id: p.display_name for p in self.pairs}

    def path_map(self) -> dict:
        """Map pair_id -> (left_path, right_path). Only populated for successful pairs."""
        return {
            r.pair.pair_id: (r.left_path, r.right_path)
            for r in self.results if r.success
        }

    def iter_successful(self):
        """Yield (pair_id, display_name, filename, left_path, right_path).

        filename is the basename shared by left and right (extension consistency
        is enforced by the downloader) and is the key used by
        FastBatchValidator.matching_files. Consumers should prefer this over
        name_map() + path_map() when both display info and file paths are needed.
        """
        for r in self.results:
            if r.success and r.left_path is not None and r.right_path is not None:
                yield (r.pair.pair_id, r.pair.display_name,
                       r.left_path.name, r.left_path, r.right_path)

    def cleanup(self):
        """Remove the entire run directory tree. Safe to call multiple times."""
        if self.base_dir and Path(self.base_dir).exists():
            shutil.rmtree(self.base_dir, ignore_errors=True)
