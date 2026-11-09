"""Concurrent downloader for DownloadPair lists.

Design:

  - Each run creates an isolated directory tree under a unique run dir.
  - Files are named pair_XXXX.<ext> on disk so FastBatchValidator can match
    them by filename intersection. Display names live only in DownloadPair
    and DownloadSession, never on disk.
  - Identical URLs across pairs are downloaded exactly once.
  - The real format is detected from the payload (magic bytes + ZIP contents),
    not just the URL suffix, so an extension-less feed endpoint or a feed
    served as ``application/octet-stream`` is still read correctly.
  - Extension mismatch between the two sides of a pair is rejected before
    any file is written.
  - Individual URL or pair failures do not abort the run.
  - Failed downloads and partial pair copies are cleaned up.
  - One ``requests.Session`` per worker thread, so TCP/TLS connections are
    reused across URLs instead of being renegotiated for every file.

Progress reporting:

  Two callbacks, both optional and both invoked from worker threads:

    ``progress_callback(ProgressUpdate)``  global bar position. Throttled to
        ``PROGRESS_INTERVAL``; ``Content-Length`` is used for a real byte
        fraction when every URL reports one, otherwise it falls back to a
        completed-file count.

    ``pair_callback(PairStatus)``  one row of the UI's pairing table. State
        transitions are emitted immediately; intermediate byte updates ride
        on the same throttle as the global bar.

  A URL that fails marks every pair referencing it as failed straight away,
  so the user sees the problem during the run instead of after it.

Cancellation:

  An optional ``cancel_event`` (``threading.Event``) enables cooperative
  cancellation. When set, the run loop breaks out of the URL fetch stage,
  pending pairs are marked ``Cancelled``, and the caller can wait() on the
  worker without needing to ``terminate()`` the QThread. In-flight HTTP
  requests abort at the next 256 KB chunk, so cancellation is bounded-latency
  rather than instantaneous — but files on disk are never left in an
  inconsistent state.
"""
import hashlib
import shutil
import tempfile
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .models import DownloadPair, DownloadResult, DownloadSession
from .progress import (
    DOWNLOAD_SPAN,
    PAIR_CANCELLED,
    PAIR_DONE,
    PAIR_DOWNLOADING,
    PAIR_FAILED,
    PAIR_PENDING,
    PHASE_DOWNLOAD,
    PairStatus,
    ProgressUpdate,
    Throttle,
    format_bytes,
)


ALLOWED_EXTENSIONS = {'.csv', '.xlsx', '.xls', '.xml', '.zip'}
DEFAULT_EXTENSION = '.csv'
CHUNK_SIZE = 256 * 1024
MAX_ATTEMPTS = 2

# OLE2 compound document header (legacy .xls)
_OLE2_MAGIC = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
_ZIP_MAGIC = b'PK\x03\x04'

_CONTENT_TYPE_EXT = {
    'text/csv': '.csv',
    'application/csv': '.csv',
    'text/plain': '.csv',
    'application/xml': '.xml',
    'text/xml': '.xml',
    'application/zip': '.zip',
    'application/x-zip-compressed': '.zip',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
    'application/vnd.ms-excel': '.xls',
}

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class DownloadError(Exception):
    """Raised when a single URL cannot be downloaded."""


class DownloadCancelled(DownloadError):
    """Raised when the run is aborted by the user."""


@dataclass
class FetchedFile:
    """A completed download plus its detected on-disk format."""
    path: Path
    extension: str


def _infer_extension(url: str) -> Optional[str]:
    """Infer a file extension from the URL path. None when unknown."""
    try:
        path = urlparse(url).path
        suffix = Path(path).suffix.lower()
        if suffix in ALLOWED_EXTENSIONS:
            return suffix
    except Exception:
        pass
    return None


def _url_key(url: str) -> str:
    """Deterministic cache filename for a URL."""
    return hashlib.sha256(url.encode('utf-8')).hexdigest()


def _sniff_extension(path: Path) -> Optional[str]:
    """Detect the real format from the payload. None when inconclusive."""
    try:
        with open(path, 'rb') as f:
            head = f.read(1024)
    except OSError:
        return None

    if head[:4] == _ZIP_MAGIC:
        # An .xlsx workbook is a ZIP; distinguish it from a plain archive of
        # feed files so the reader does not go looking for members inside it.
        try:
            with zipfile.ZipFile(path) as zf:
                names = zf.namelist()
        except Exception:
            return '.zip'
        if any(n.lower() == 'xl/workbook.xml' for n in names):
            return '.xlsx'
        return '.zip'

    if head[:8] == _OLE2_MAGIC:
        return '.xls'

    body = head.lstrip(b'\xef\xbb\xbf').lstrip()
    if body.startswith(b'<?xml') or body.startswith(b'<'):
        return '.xml'

    return None


class BatchDownloader:
    """Download a list of DownloadPair concurrently with per-run isolation."""

    def __init__(self, headers: Optional[dict] = None,
                 max_workers: int = 4, timeout: int = 120,
                 verify_ssl: bool = True,
                 cancel_event: Optional[threading.Event] = None):
        self.headers = headers or {}
        self.max_workers = max(1, int(max_workers))
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.cancel_event = cancel_event
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()

        # Progress state, guarded by _state_lock. Every field is written from
        # the pool threads, so all access goes through the lock.
        self._state_lock = threading.Lock()
        self._url_bytes: Dict[str, int] = {}
        self._url_total: Dict[str, int] = {}
        self._url_done = set()
        self._url_to_pairs: Dict[str, list] = {}
        self._pairs: list = []
        self._finalized = set()
        self._pair_last_pct: Dict[int, int] = {}
        self._progress_cb = None
        self._pair_cb = None
        self._throttle = Throttle()
        self._base = 0.0
        self._span = DOWNLOAD_SPAN

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    # ------------------------------------------------------------------
    # HTTP plumbing
    # ------------------------------------------------------------------

    def _session(self) -> requests.Session:
        """Per-thread session so connections are reused within a worker."""
        session = getattr(self._local, 'session', None)
        if session is None:
            session = requests.Session()
            session.headers.update(self.headers)
            adapter = HTTPAdapter(
                pool_connections=self.max_workers,
                pool_maxsize=self.max_workers,
                max_retries=0,
            )
            session.mount('https://', adapter)
            session.mount('http://', adapter)
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return session

    def _close_sessions(self) -> None:
        with self._sessions_lock:
            sessions, self._sessions = self._sessions, []
        for session in sessions:
            try:
                session.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Progress bookkeeping
    # ------------------------------------------------------------------

    def _emit_progress(self, force: bool = False, complete: bool = False) -> None:
        """Publish the global bar position, at most every PROGRESS_INTERVAL.

        ``complete=True`` forces the phase to 100%, so the download segment of
        the bar is always filled before validation starts even when every
        download failed.
        """
        if self._progress_cb is None or not self._throttle.allow(force):
            return

        with self._state_lock:
            urls = list(self._url_bytes)
            done_bytes = sum(self._url_bytes.values())
            total_bytes = sum(self._url_total.get(u, 0) for u in urls)
            done_urls = len(self._url_done)
            all_sized = bool(urls) and all(
                self._url_total.get(u, 0) > 0 for u in urls
            )
            pairs_total = len(self._pairs)
            pairs_done = 0
            active_label = ''
            for pair in self._pairs:
                left, right = pair.left_url, pair.right_url
                if left and right and left in self._url_done and right in self._url_done:
                    pairs_done += 1
                elif not active_label:
                    active_label = pair.display_name

        if all_sized and total_bytes > 0:
            computed = done_bytes / total_bytes
            detail = f"{format_bytes(done_bytes)} / {format_bytes(total_bytes)}"
        else:
            computed = (done_urls / len(urls)) if urls else 0.0
            detail = f"{done_urls}/{len(urls)} files"
        fraction = 1.0 if complete else computed

        self._progress_cb(ProgressUpdate(
            phase=PHASE_DOWNLOAD,
            index=pairs_done,
            total=pairs_total,
            label=active_label,
            fraction=fraction,
            detail=detail,
            base=self._base,
            span=self._span,
        ))

        # Per-pair rows ride on the same tick. Only emit when the integer
        # percentage actually moved, otherwise every tick rewrites every row.
        for index, pair in enumerate(self._pairs):
            if index in self._finalized:
                continue
            fraction, detail = self._pair_progress(pair)
            percent = int(fraction * 100)
            if self._pair_last_pct.get(index) == percent:
                continue
            self._pair_last_pct[index] = percent
            self._emit_pair(index, PAIR_DOWNLOADING, fraction, detail)

    def _pair_progress(self, pair: DownloadPair):
        """Fraction and byte detail for one pair, across both of its URLs."""
        urls = [u for u in (pair.left_url, pair.right_url) if u]
        if not urls:
            return 0.0, ''
        with self._state_lock:
            sized = all(self._url_total.get(u, 0) > 0 for u in urls)
            if sized:
                total = sum(self._url_total[u] for u in urls)
                done = sum(self._url_bytes.get(u, 0) for u in urls)
                if total > 0:
                    return (
                        min(done / total, 1.0),
                        f"{format_bytes(done)} / {format_bytes(total)}",
                    )
            done_urls = sum(1 for u in urls if u in self._url_done)
        return done_urls / len(urls), f"{done_urls}/{len(urls)}"

    def _emit_pair(self, index: int, state: str, fraction: float = 0.0,
                   detail: str = '', error: str = '') -> None:
        if self._pair_cb is None:
            return
        self._pair_cb(PairStatus(
            index=index, state=state, fraction=fraction,
            detail=detail, error=error,
        ))

    def _report_bytes(self, url: str, count: int) -> None:
        with self._state_lock:
            self._url_bytes[url] = self._url_bytes.get(url, 0) + count
        self._emit_progress()

    def _reset_url_bytes(self, url: str, total: int = 0) -> None:
        with self._state_lock:
            self._url_bytes[url] = 0
            self._url_total[url] = total

    def _mark_url_done(self, url: str) -> None:
        with self._state_lock:
            self._url_done.add(url)
        self._emit_progress(force=True)

    def _fail_url_pairs(self, url: str, error: str) -> None:
        """Fail every pair that depends on ``url`` immediately."""
        for index in list(self._url_to_pairs.get(url, [])):
            if index in self._finalized:
                continue
            self._finalized.add(index)
            self._emit_pair(index, PAIR_FAILED, 0.0, '', error)

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(self, session: DownloadSession, base_root: Path,
            progress_callback: Optional[Callable[[ProgressUpdate], None]] = None,
            pair_callback: Optional[Callable[[PairStatus], None]] = None,
            base: float = 0.0, span: float = DOWNLOAD_SPAN) -> DownloadSession:
        """Download all pairs in session and populate session.results.

        If cancel_event is set mid-run, the URL loop breaks, remaining URLs
        are marked cancelled, and the function returns normally with a
        populated session (successful/failed splits reflect what completed).
        """
        self._progress_cb = progress_callback
        self._pair_cb = pair_callback
        self._base = base
        self._span = span

        base_root = Path(base_root)
        base_root.mkdir(parents=True, exist_ok=True)
        run_dir = Path(tempfile.mkdtemp(dir=base_root, prefix="run_"))
        cache_dir = run_dir / "_cache"
        left_dir = run_dir / "left"
        right_dir = run_dir / "right"
        cache_dir.mkdir(exist_ok=True)
        left_dir.mkdir(exist_ok=True)
        right_dir.mkdir(exist_ok=True)
        session.base_dir = run_dir

        for idx, pair in enumerate(session.pairs):
            pair.pair_id = f"pair_{idx + 1:04d}"

        unique_urls = []
        seen = set()
        self._pairs = list(session.pairs)
        self._url_to_pairs = {}
        self._finalized = set()
        self._pair_last_pct = {}
        for index, pair in enumerate(session.pairs):
            for url in (pair.left_url, pair.right_url):
                if not url:
                    continue
                self._url_to_pairs.setdefault(url, []).append(index)
                if url not in seen:
                    seen.add(url)
                    unique_urls.append(url)

        self._url_bytes = {url: 0 for url in unique_urls}
        self._url_total = {url: 0 for url in unique_urls}
        self._url_done = set()

        for index in range(len(session.pairs)):
            self._emit_pair(index, PAIR_PENDING, 0.0, '')
        self._emit_progress(force=True)

        url_to_file: Dict[str, FetchedFile] = {}
        url_to_error: Dict[str, str] = {}
        total_urls = len(unique_urls)
        completed = 0

        try:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {
                    pool.submit(self._download_one, url, cache_dir / _url_key(url)): url
                    for url in unique_urls
                }
                for future in as_completed(futures):
                    if self._is_cancelled():
                        # Cancel anything not yet started. Running futures
                        # abort at their next chunk boundary.
                        for f in futures:
                            f.cancel()
                        break

                    url = futures[future]
                    completed += 1
                    try:
                        url_to_file[url] = future.result()
                        self._mark_url_done(url)
                    except DownloadCancelled:
                        url_to_error[url] = "Cancelled"
                        self._fail_url_pairs(url, "Cancelled")
                    except DownloadError as e:
                        url_to_error[url] = str(e)
                        self._fail_url_pairs(url, str(e))
                    except Exception as e:
                        url_to_error[url] = f"Unexpected: {e}"
                        self._fail_url_pairs(url, str(e))
        finally:
            self._close_sessions()

        cancelled_run = self._is_cancelled()
        results = []
        for index, pair in enumerate(session.pairs):
            if cancelled_run and pair.left_url not in url_to_file \
                    and pair.right_url not in url_to_file:
                results.append(DownloadResult(
                    pair=pair, success=False, error="Cancelled", cancelled=True,
                ))
                if index not in self._finalized:
                    self._finalized.add(index)
                    self._emit_pair(index, PAIR_CANCELLED, 0.0, 'Cancelled')
                continue

            result = self._assemble_pair(
                pair, url_to_file, url_to_error, left_dir, right_dir,
            )
            results.append(result)
            if index not in self._finalized:
                self._finalized.add(index)
                if result.success:
                    self._emit_pair(index, PAIR_DONE, 1.0, '')
                elif result.cancelled:
                    self._emit_pair(index, PAIR_CANCELLED, 0.0, 'Cancelled')
                else:
                    self._emit_pair(index, PAIR_FAILED, 0.0, '', result.error)

        self._emit_progress(force=True, complete=True)
        session.results = results
        return session

    # ------------------------------------------------------------------
    # Single URL
    # ------------------------------------------------------------------

    def _download_one(self, url: str, cache_path: Path) -> FetchedFile:
        """Download a URL to cache_path. Raises DownloadError on any failure.

        On any failure (including cancellation) the partially written cache
        file is removed so _cache/ only contains complete downloads.
        """
        if self._is_cancelled():
            raise DownloadCancelled("Cancelled")

        last_error = "unknown error"
        for attempt in range(MAX_ATTEMPTS):
            if self._is_cancelled():
                raise DownloadCancelled("Cancelled")
            self._reset_url_bytes(url)
            try:
                return self._attempt_download(url, cache_path)
            except DownloadCancelled:
                raise
            except DownloadError as e:
                last_error = str(e)
                cache_path.unlink(missing_ok=True)
                # Only transient failures are worth a second attempt.
                if attempt + 1 >= MAX_ATTEMPTS or not getattr(e, 'retryable', False):
                    raise
        raise DownloadError(last_error)

    def _attempt_download(self, url: str, cache_path: Path) -> FetchedFile:
        session = self._session()
        try:
            resp = session.get(
                url,
                timeout=self.timeout,
                verify=self.verify_ssl,
                stream=True,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            err = DownloadError(f"Request failed: {e}")
            err.retryable = True
            raise err

        try:
            if resp.status_code != 200:
                err = DownloadError(f"HTTP {resp.status_code}")
                err.retryable = resp.status_code in _RETRYABLE_STATUS
                raise err

            try:
                declared = int(resp.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > 0:
                self._reset_url_bytes(url, declared)

            with open(cache_path, 'wb') as f:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if self._is_cancelled():
                        raise DownloadCancelled("Cancelled")
                    if chunk:
                        f.write(chunk)
                        self._report_bytes(url, len(chunk))
        except DownloadCancelled:
            cache_path.unlink(missing_ok=True)
            raise
        except requests.RequestException as e:
            cache_path.unlink(missing_ok=True)
            err = DownloadError(f"Stream failed: {e}")
            err.retryable = True
            raise err
        except OSError as e:
            cache_path.unlink(missing_ok=True)
            raise DownloadError(f"Write failed: {e}")
        finally:
            resp.close()

        return FetchedFile(
            path=cache_path,
            extension=self._resolve_extension(url, cache_path, resp),
        )

    @staticmethod
    def _resolve_extension(url: str, path: Path, resp) -> str:
        """Decide the on-disk extension from payload, URL and headers."""
        url_ext = _infer_extension(url)
        sniffed = _sniff_extension(path)

        if sniffed == '.zip' and url_ext in {'.xlsx', '.xls'}:
            return url_ext
        if sniffed is not None:
            return sniffed
        if url_ext is not None:
            return url_ext

        content_type = (resp.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        return _CONTENT_TYPE_EXT.get(content_type, DEFAULT_EXTENSION)

    # ------------------------------------------------------------------
    # Pair assembly
    # ------------------------------------------------------------------

    def _assemble_pair(self, pair: DownloadPair,
                       url_to_file: Dict[str, FetchedFile],
                       url_to_error: Dict[str, str],
                       left_dir: Path, right_dir: Path) -> DownloadResult:
        """Copy a downloaded pair into left/ and right/ with a unified name."""
        if not pair.left_url or not pair.right_url:
            return DownloadResult(pair=pair, success=False,
                                  error="Empty URL on one side")

        if pair.left_url in url_to_error:
            error = url_to_error[pair.left_url]
            return DownloadResult(pair=pair, success=False,
                                  error=f"Left: {error}",
                                  cancelled=error == "Cancelled")
        if pair.right_url in url_to_error:
            error = url_to_error[pair.right_url]
            return DownloadResult(pair=pair, success=False,
                                  error=f"Right: {error}",
                                  cancelled=error == "Cancelled")

        left_src = url_to_file.get(pair.left_url)
        right_src = url_to_file.get(pair.right_url)
        if left_src is None or right_src is None:
            # Not a cache bug: this is what a cancelled run leaves behind.
            return DownloadResult(
                pair=pair, success=False, cancelled=True,
                error="Not downloaded (cancelled or interrupted)",
            )

        if left_src.extension != right_src.extension:
            return DownloadResult(
                pair=pair, success=False,
                error=(f"Extension mismatch: {left_src.extension} vs "
                       f"{right_src.extension} (the two sides must share a format)"),
            )

        pair_tag = pair.pair_id
        left_dst = left_dir / f"{pair_tag}{left_src.extension}"
        right_dst = right_dir / f"{pair_tag}{right_src.extension}"

        try:
            shutil.copyfile(left_src.path, left_dst)
        except OSError as e:
            return DownloadResult(pair=pair, success=False,
                                  error=f"Left copy failed: {e}")

        try:
            shutil.copyfile(right_src.path, right_dst)
        except OSError as e:
            left_dst.unlink(missing_ok=True)
            return DownloadResult(pair=pair, success=False,
                                  error=f"Right copy failed: {e}")

        return DownloadResult(
            pair=pair, success=True,
            left_path=left_dst, right_path=right_dst,
        )
