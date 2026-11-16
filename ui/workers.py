"""QThread workers bridging UI and core layer.

Cancellation contract:

  - Every long-running worker exposes ``request_stop()`` which sets an
    internal ``threading.Event``. The worker passes this event to core
    objects (BatchDownloader / FastBatchValidator), which check it at
    natural yield points and exit gracefully.
  - Callers should ``request_stop()`` then ``wait(timeout)`` instead of
    ``terminate()``. ``terminate()`` can leave files half-written.
  - A cancelled run always emits ``cancelled`` before returning. Emitting
    nothing would leave the UI permanently in its "running" state, which is
    exactly what the previous implementation did.
  - Fetch* workers are short HTTP requests and are bounded by ``timeout``.

Threading contract:

  - Workers never touch Qt widgets. They only emit signals.
  - All core-layer objects live entirely inside the worker thread.
  - DownloadPair objects are deep-copied before handoff so the downloader's
    pair_id rewrite does not mutate objects owned by the UI layer.

Signal naming: the custom result signal is ``results_ready``, not
``finished``. ``QThread`` already defines ``finished``; shadowing it silently
replaces the thread-lifecycle signal and makes ``deleteLater`` wiring
impossible to reason about.
"""

import copy
import tempfile
import threading
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import requests
from PySide6.QtCore import QThread, Signal

from core.downloader import BatchDownloader
from core.models import DownloadPair, DownloadSession, PairSource
from core.progress import DOWNLOAD_SPAN, VALIDATE_SPAN
from core.validator import FastBatchValidator


def folder_labels(path1: str, path2: str) -> Tuple[str, str]:
    """Short, unambiguous labels for two folder paths.

    File mode used to report empty site labels, so every result table fell back
    to generic "Value F1" / "Value F2" headings and there was no way to tell
    which folder a value came from. The last path component is what a user
    recognises; if both sides share it (``.../old/site_a`` vs ``.../new/site_a``)
    the parent directory is prepended so the two sides stay distinguishable.
    """

    def short(path_text: str) -> str:
        path = Path(path_text)
        return path.name or path.drive or str(path)

    first, second = short(path1), short(path2)
    if first == second:
        parent1, parent2 = Path(path1).parent.name, Path(path2).parent.name
        if parent1:
            first = f"{parent1}/{first}"
        if parent2:
            second = f"{parent2}/{second}"
    if first == second:
        # Still identical (e.g. two drive roots): fall back to the full path.
        first, second = str(Path(path1)), str(Path(path2))
    return first, second


def destination_url(feed_destinations) -> str:
    """First downloadable URL among a channel's feed destinations.

    The platform keys destinations by destination id:

        {"4711": {"active": true, "urls": ["https://..."]}}

    Older responses put a plain URL list under the key ``"2"``, so both
    shapes are handled. Only an *active* destination with a non-empty url
    list counts — taking an inactive one would download a feed the channel
    no longer publishes. When several are active the first wins, which
    matches the rule the comparison tool uses for its GOS channel.
    """
    if not isinstance(feed_destinations, dict):
        return ""

    for destination in feed_destinations.values():
        if isinstance(destination, dict) and destination.get("active"):
            urls = destination.get("urls")
            if isinstance(urls, list) and urls:
                return str(urls[0])

    legacy = feed_destinations.get("2")
    if isinstance(legacy, list) and legacy:
        return str(legacy[0])
    if isinstance(legacy, str) and legacy:
        return legacy
    return ""


# Platform metadata calls are small JSON requests; a bounded timeout keeps a
# dead endpoint from blocking a worker thread forever.
API_TIMEOUT = 30
DOWNLOAD_ROOT = Path(tempfile.gettempdir()) / "feed_validator"


# ---------------------------------------------------------------------------
# Platform API workers (projects / sites / channels) - short, non-cancellable
# ---------------------------------------------------------------------------


class _FetchWorker(QThread):
    """Shared plumbing for the short platform-metadata requests."""

    log = Signal(str)
    data_ready = Signal(list)
    error = Signal(str)

    def __init__(self, base_url: str, headers: dict, verify_ssl: bool = True):
        super().__init__()
        self.base_url = (base_url or "").rstrip("/")
        self.headers = headers or {}
        self.verify_ssl = verify_ssl

    def _get_json(self, path: str) -> dict:
        resp = requests.get(
            f"{self.base_url}{path}",
            headers=self.headers,
            timeout=API_TIMEOUT,
            verify=self.verify_ssl,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code} from {path}")
        payload = resp.json()
        if not payload.get("success"):
            raise RuntimeError(f"API reported failure for {path}")
        return payload


class FetchProjectsWorker(_FetchWorker):
    def run(self):
        try:
            self.log.emit("Fetching project list...")
            payload = self._get_json("/projects")
            projects = payload.get("Projects") or []
            project_list = [
                {"id": p["id"], "name": p.get("name") or f"Project_{p['id']}"}
                for p in projects
            ]
            self.log.emit(f"Loaded {len(project_list)} projects.")
            self.data_ready.emit(project_list)
        except Exception as e:
            self.error.emit(str(e))


class FetchSitesWorker(_FetchWorker):
    def __init__(
        self,
        base_url: str,
        headers: dict,
        project_id,
        only_active: bool = False,
        verify_ssl: bool = True,
    ):
        super().__init__(base_url, headers, verify_ssl)
        self.project_id = project_id
        self.only_active = only_active

    def run(self):
        try:
            self.log.emit("Fetching sites...")
            payload = self._get_json(f"/projects/{self.project_id}/sites")
            sites = payload.get("Sites") or []
            if self.only_active:
                sites = [s for s in sites if s.get("status") == "active"]
            seen = set()
            unique_sites = []
            for s in sites:
                sid = s.get("id")
                if sid is None or sid in seen:
                    continue
                seen.add(sid)
                name = s.get("title") or s.get("name") or f"Site_{sid}"
                unique_sites.append(
                    {
                        "id": sid,
                        "name": name,
                        "status": s.get("status", "unknown"),
                    }
                )
            self.log.emit(f"Fetched {len(unique_sites)} unique sites.")
            self.data_ready.emit(unique_sites)
        except Exception as e:
            self.error.emit(str(e))


class FetchChannelsWorker(_FetchWorker):
    def __init__(self, base_url: str, headers: dict, site_id, verify_ssl: bool = True):
        super().__init__(base_url, headers, verify_ssl)
        self.site_id = site_id

    def run(self):
        try:
            self.log.emit("Fetching channels...")
            payload = self._get_json(f"/sites/{self.site_id}/channels")
            channels = payload.get("Channels") or []
            channel_list = []
            for ch in channels:
                channel_list.append(
                    {
                        "name": ch.get("name") or f"channel_{len(channel_list) + 1}",
                        "link": destination_url(ch.get("feed_destinations")),
                    }
                )
            self.log.emit(f"Fetched {len(channel_list)} channels.")
            self.data_ready.emit(channel_list)
        except Exception as e:
            self.error.emit(str(e))


# ---------------------------------------------------------------------------
# Download + compare worker
# ---------------------------------------------------------------------------


class DownloadAndCompareWorker(QThread):
    """Download a list of pairs, then validate the downloaded tree.

    Emits exactly one of ``results_ready`` / ``error`` / ``cancelled``.
    """

    log = Signal(str)
    progress = Signal(object)  # ProgressUpdate
    pair_status = Signal(object)  # PairStatus
    results_ready = Signal(dict)
    error = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        pairs: List[DownloadPair],
        site1_label: str,
        site2_label: str,
        source: PairSource,
        headers: dict,
        verify_ssl: bool,
        sku_col: Optional[str],
        compare_cols: List[str],
        normalize_sku: bool,
        case_sensitive: bool,
        debug: bool,
        auto_detect: bool,
        priority_list: Optional[List[str]],
        exclude_keywords: Optional[List[str]],
        export_diff: bool = True,
        download_workers: int = 4,
        validate_workers: int = 4,
        download_root: Optional[Path] = None,
        download_timeout: int = 120,
    ):
        super().__init__()
        self.pairs = [copy.deepcopy(p) for p in pairs]
        self.site1_label = site1_label
        self.site2_label = site2_label
        self.source = source
        self.headers = headers
        self.verify_ssl = verify_ssl
        self.sku_col = sku_col
        self.compare_cols = compare_cols
        self.normalize_sku = normalize_sku
        self.case_sensitive = case_sensitive
        self.debug = debug
        self.auto_detect = auto_detect
        self.priority_list = priority_list
        self.exclude_keywords = exclude_keywords
        self.export_diff = bool(export_diff)
        self.download_workers = download_workers
        self.validate_workers = validate_workers
        self.download_timeout = download_timeout
        self.download_root = Path(download_root) if download_root else DOWNLOAD_ROOT
        self._cancel_event = threading.Event()

    def request_stop(self) -> None:
        """Signal the worker to stop. Thread-safe. Idempotent."""
        self._cancel_event.set()

    def _is_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self):
        session = None
        validator = None
        try:
            if not self.pairs:
                self.error.emit("No pairs to download.")
                return

            # ---------- Phase 1: download ----------
            self.log.emit(f"Downloading {len(self.pairs)} pairs...")
            session = DownloadSession(
                source=self.source,
                site1_label=self.site1_label,
                site2_label=self.site2_label,
                pairs=list(self.pairs),
            )
            downloader = BatchDownloader(
                headers=self.headers,
                max_workers=self.download_workers,
                timeout=self.download_timeout,
                verify_ssl=self.verify_ssl,
                cancel_event=self._cancel_event,
            )
            session = downloader.run(
                session,
                self.download_root,
                progress_callback=self.progress.emit,
                pair_callback=self.pair_status.emit,
                base=0.0,
                span=DOWNLOAD_SPAN,
            )

            if self._is_cancelled():
                self.log.emit("Cancelled during download.")
                session.cleanup()
                self.cancelled.emit()
                return

            if not session.successful:
                self.log.emit(
                    f"All {len(session.pairs)} pair(s) failed to download. "
                    "See Files tab for per-pair errors."
                )
                empty_results = {
                    "files": {},
                    "errors": [],
                    "skipped": [],
                    "overall": {
                        "total_pairs": len(session.pairs),
                        "success": 0,
                        "failed": 0,
                        "skipped": 0,
                        "total_skus": 0,
                        "total_diffs": 0,
                        "total_missing": 0,
                        "avg_match_rate": 0,
                    },
                    "overall_column_stats": {},
                    "debug_logs": [],
                    "sku_detection_log": [],
                    "temp_diff_dir": "",
                }
                merged = self._merge_results(empty_results, session)
                merged["cleanup"] = session.cleanup
                merged["site1_label"] = self.site1_label
                merged["site2_label"] = self.site2_label
                merged["source"] = self.source.value
                merged["diff_export_enabled"] = self.export_diff
                self.results_ready.emit(merged)
                return

            self.log.emit(
                f"Downloaded {len(session.successful)}/{len(session.pairs)} pairs."
            )

            # ---------- Phase 2: validate ----------
            validator = FastBatchValidator(
                str(session.base_dir / "left"),
                str(session.base_dir / "right"),
                sku_column=self.sku_col,
                compare_columns=self.compare_cols,
                max_workers=self.validate_workers,
                normalize_sku=self.normalize_sku,
                case_sensitive=self.case_sensitive,
                debug=self.debug,
                auto_detect_sku=self.auto_detect,
                priority_list=self.priority_list,
                exclude_keywords=self.exclude_keywords,
                export_diff=self.export_diff,
            )
            validator_results = validator.validate_all_parallel(
                progress_callback=self.progress.emit,
                cancel_event=self._cancel_event,
                progress_base=DOWNLOAD_SPAN,
                progress_span=VALIDATE_SPAN,
            )

            if self._is_cancelled():
                self.log.emit("Cancelled during validation.")
                validator.cleanup()
                session.cleanup()
                self.cancelled.emit()
                return

            # ---------- Phase 3: merge ----------
            merged = self._merge_results(validator_results, session)

            def _cleanup(_validator=validator, _session=session):
                _validator.cleanup()
                _session.cleanup()

            merged["cleanup"] = _cleanup
            merged["site1_label"] = self.site1_label
            merged["site2_label"] = self.site2_label
            merged["source"] = self.source.value
            merged["diff_export_enabled"] = self.export_diff

            self.results_ready.emit(merged)

        except Exception as e:
            if validator is not None:
                try:
                    validator.cleanup()
                except Exception:
                    pass
            if session is not None:
                try:
                    session.cleanup()
                except Exception:
                    pass
            self.error.emit(f"{e}\n\n{traceback.format_exc()}")

    def _merge_results(self, validator_results: dict, session: DownloadSession) -> dict:
        """Map validator filename keys to user-visible display names.

        Conflicts on display_name (e.g. two manual pairs configured identically)
        are disambiguated by appending the pair_id rather than overwriting.

        Overall counters:
          - failed            : validation failures only (matches errors list)
          - validation_failed : explicit alias of failed
          - download_failed   : pairs that failed before reaching validation
          - total_failed      : sum of the two, for display purposes
        """
        filename_to_info = {}
        for pair_id, display_name, filename, _left, _right in session.iter_successful():
            filename_to_info[filename] = (display_name, pair_id)

        merged_files = {}
        for filename, result in validator_results.get("files", {}).items():
            display_name, pair_id = filename_to_info.get(filename, (filename, filename))
            key = self._dedup_key(merged_files, display_name, pair_id)
            result["file"] = display_name
            result["pair_id"] = pair_id
            merged_files[key] = result

        for r in session.failed:
            display_name = r.pair.display_name
            pair_id = r.pair.pair_id
            key = self._dedup_key(merged_files, display_name, pair_id)
            merged_files[key] = {
                "status": "download_failed",
                "file": display_name,
                "pair_id": pair_id,
                "error": r.error,
                "left_url": r.pair.left_url,
                "right_url": r.pair.right_url,
                "column_stats": {},
                "summary": {},
            }

        merged_files = dict(
            sorted(
                merged_files.items(),
                key=lambda kv: kv[1].get("pair_id", ""),
            )
        )

        overall = dict(validator_results.get("overall", {}))
        validation_failed = overall.get("failed", 0)
        download_failed = len(session.failed)
        overall["total_pairs"] = len(session.pairs)
        overall["validation_failed"] = validation_failed
        overall["download_failed"] = download_failed
        overall["total_failed"] = validation_failed + download_failed

        validator_results["files"] = merged_files
        validator_results["overall"] = overall
        return validator_results

    @staticmethod
    def _dedup_key(existing: dict, display_name: str, pair_id: str) -> str:
        """Return a unique key, appending pair_id on collision."""
        if display_name not in existing:
            return display_name
        return f"{display_name} [{pair_id}]"


# ---------------------------------------------------------------------------
# File-mode direct validation worker
# ---------------------------------------------------------------------------


class ValidationWorker(QThread):
    """QThread wrapper for File-mode direct validation (no download phase).

    Emits exactly one of ``results_ready`` / ``error`` / ``cancelled``.
    """

    progress = Signal(object)  # ProgressUpdate
    results_ready = Signal(dict)
    error = Signal(str)
    cancelled = Signal()

    def __init__(self, folder1: str, folder2: str, config: dict):
        super().__init__()
        self.folder1 = folder1
        self.folder2 = folder2
        self.config = dict(config)
        self._cancel_event = threading.Event()

    def request_stop(self) -> None:
        """Signal the worker to stop. Thread-safe. Idempotent."""
        self._cancel_event.set()

    def run(self):
        validator = None
        try:
            validator = FastBatchValidator(
                self.folder1,
                self.folder2,
                sku_column=self.config.get("sku_column"),
                compare_columns=self.config.get("compare_columns") or [],
                file_pattern=self.config.get("file_filter"),
                max_workers=self.config.get("workers", 4),
                normalize_sku=self.config.get("normalize_sku", True),
                case_sensitive=self.config.get("case_sensitive", True),
                debug=self.config.get("debug", True),
                auto_detect_sku=self.config.get("auto_detect", True),
                priority_list=self.config.get("priority_list"),
                exclude_keywords=self.config.get("exclude_keywords"),
                export_diff=self.config.get("export_diff", True),
            )
            results = validator.validate_all_parallel(
                progress_callback=self.progress.emit,
                cancel_event=self._cancel_event,
                progress_base=0.0,
                progress_span=1.0,
            )

            if self._cancel_event.is_set():
                validator.cleanup()
                self.cancelled.emit()
                return

            results["cleanup"] = validator.cleanup
            # Name the two sides after their folders; without this every
            # result table falls back to "Value F1" / "Value F2".
            results["site1_label"], results["site2_label"] = folder_labels(
                self.folder1, self.folder2
            )
            results["site1_path"] = self.folder1
            results["site2_path"] = self.folder2
            results["source"] = "file"
            results["diff_export_enabled"] = self.config.get(
                "diff_export_enabled", True
            )
            self.results_ready.emit(results)
        except Exception as e:
            if validator is not None:
                try:
                    validator.cleanup()
                except Exception:
                    pass
            self.error.emit(f"{e}\n\n{traceback.format_exc()}")
