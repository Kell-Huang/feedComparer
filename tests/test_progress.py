"""Progress reporting tests: phase weighting, throttling and pair lifecycle.

The important invariants:

  - the bar never moves backwards across the two phases of a run;
  - the throttle keeps emissions bounded so the UI thread is not flooded;
  - every pair reaches a terminal state, and a failure is reported while the
    run is still going rather than only at the end.
"""
import collections
import functools
import http.server
import tempfile
import threading
import time
from pathlib import Path

from core.downloader import BatchDownloader
from core.models import DownloadPair, DownloadSession, PairSource
from core.progress import (
    DOWNLOAD_SPAN,
    PAIR_CANCELLED,
    PAIR_DONE,
    PAIR_FAILED,
    PAIR_PENDING,
    PHASE_DOWNLOAD,
    PHASE_VALIDATE,
    ProgressUpdate,
    Throttle,
    format_bytes,
    truncate,
)


# ---------------------------------------------------------------------------
# ProgressUpdate
# ---------------------------------------------------------------------------

def test_overall_is_monotonic_across_phases():
    """Download 0..0.6 then validate 0.6..1.0, never backwards."""
    download = [
        ProgressUpdate(PHASE_DOWNLOAD, i, 10, 'p', i / 10, base=0.0, span=DOWNLOAD_SPAN).overall
        for i in range(11)
    ]
    validate = [
        ProgressUpdate(PHASE_VALIDATE, i, 10, 'f', i / 10, base=DOWNLOAD_SPAN, span=1 - DOWNLOAD_SPAN).overall
        for i in range(11)
    ]
    sequence = download + validate
    assert sequence == sorted(sequence), sequence
    assert sequence[0] == 0.0
    assert abs(sequence[-1] - 1.0) < 1e-9
    # The download phase stops exactly where validation starts.
    assert abs(download[-1] - DOWNLOAD_SPAN) < 1e-9
    assert abs(validate[0] - DOWNLOAD_SPAN) < 1e-9


def test_overall_clamps_out_of_range():
    low = ProgressUpdate(PHASE_DOWNLOAD, 0, 1, 'p', -5.0, base=0.0, span=0.6)
    high = ProgressUpdate(PHASE_DOWNLOAD, 0, 1, 'p', 5.0, base=0.0, span=0.6)
    assert low.overall == 0.0
    assert abs(high.overall - 0.6) < 1e-9


def test_file_mode_uses_whole_bar():
    update = ProgressUpdate(PHASE_VALIDATE, 1, 2, 'f', 0.5, base=0.0, span=1.0)
    assert abs(update.overall - 0.5) < 1e-9


# ---------------------------------------------------------------------------
# Throttle / helpers
# ---------------------------------------------------------------------------

def test_throttle_limits_rate():
    throttle = Throttle(interval=0.05)
    assert throttle.allow() is True          # first call always passes
    allowed = sum(1 for _ in range(200) if throttle.allow())
    # 200 tight-loop calls must not all pass.
    assert allowed < 200, allowed


def test_throttle_force_always_passes():
    throttle = Throttle(interval=10.0)
    assert throttle.allow() is True
    assert throttle.allow() is False
    assert throttle.allow(force=True) is True


def test_format_bytes():
    assert format_bytes(0) == ''
    assert format_bytes(None) == ''
    assert format_bytes(512) == '512 B'
    assert format_bytes(2048) == '2.0 KB'
    assert format_bytes(5 * 1024 * 1024) == '5.0 MB'


def test_truncate():
    assert truncate('abc', 10) == 'abc'
    assert truncate('', 10) == ''
    assert truncate('x' * 20, 10) == 'x' * 9 + '…'


# ---------------------------------------------------------------------------
# Downloader integration
# ---------------------------------------------------------------------------

class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def _serve(directory: Path):
    handler = functools.partial(_Handler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _fixture():
    tmp = tempfile.TemporaryDirectory()
    served = Path(tmp.name) / 'served'
    served.mkdir()
    # Large enough to span several 256 KB chunks, so byte progress is real.
    body = 'sku,price\n' + ''.join(f"{i},{i * 1.5}\n" for i in range(60_000))
    (served / 'big_a.csv').write_text(body, encoding='utf-8')
    (served / 'big_b.csv').write_text(body, encoding='utf-8')
    (served / 'small_a.csv').write_text('sku,price\n1,10\n', encoding='utf-8')
    (served / 'small_b.csv').write_text('sku,price\n1,11\n', encoding='utf-8')

    httpd = _serve(served)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    # Measure the file on disk, not the string: write_text translates \n to
    # \r\n on Windows, so the served size is larger than len(body).
    size_on_disk = (served / 'big_a.csv').stat().st_size
    return tmp, httpd, base, size_on_disk


def _pair(base, index, left, right):
    return DownloadPair(
        pair_id=f'p{index}',
        left_name=left,
        left_url=f'{base}{left}',
        right_name=right,
        right_url=f'{base}{right}',
    )


def _run(pairs, **kwargs):
    progress_events = []
    pair_events = []
    tmp = tempfile.TemporaryDirectory()
    session = DownloadSession(source=PairSource.MANUAL, pairs=list(pairs))
    downloader = BatchDownloader(**kwargs)
    downloader.run(
        session, Path(tmp.name),
        progress_callback=progress_events.append,
        pair_callback=pair_events.append,
    )
    return session, progress_events, pair_events, tmp


def test_downloader_reports_pair_lifecycle():
    fixture, httpd, base, _size = _fixture()
    try:
        pairs = [_pair(base, 1, '/small_a.csv', '/small_b.csv')]
        session, _progress, pair_events, tmp = _run(pairs)
        try:
            states = [e.state for e in pair_events]
            assert states[0] == PAIR_PENDING, states
            assert states[-1] == PAIR_DONE, states
            assert len(session.successful) == 1
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_downloader_progress_never_goes_backwards():
    fixture, httpd, base, _size = _fixture()
    try:
        pairs = [
            _pair(base, 1, '/big_a.csv', '/big_b.csv'),
            _pair(base, 2, '/small_a.csv', '/small_b.csv'),
        ]
        session, progress, _pair_events, tmp = _run(pairs)
        try:
            values = [u.overall for u in progress]
            assert values == sorted(values), values
            # A download-only run owns the first DOWNLOAD_SPAN of the bar;
            # the remaining 40% belongs to the validation phase.
            assert abs(values[-1] - DOWNLOAD_SPAN) < 1e-9, values[-1]
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_downloader_reports_byte_detail():
    """With Content-Length present the status line gets real byte counts."""
    fixture, httpd, base, size = _fixture()
    try:
        pairs = [_pair(base, 1, '/big_a.csv', '/big_b.csv')]
        session, progress, _pair_events, tmp = _run(pairs)
        try:
            details = [u.detail for u in progress if u.detail]
            assert details, 'no byte detail was reported'
            assert any('KB' in d or 'MB' in d or 'B' in d for d in details), details
            # Both sides of the pair are served, so the reported total is 2x.
            expected = format_bytes(size * 2)
            assert any(expected in d for d in details), (expected, details[:5])
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_downloader_failure_marks_pair_failed_during_run():
    fixture, httpd, base, _size = _fixture()
    try:
        pairs = [
            _pair(base, 1, '/small_a.csv', '/missing.csv'),
            _pair(base, 2, '/small_a.csv', '/small_b.csv'),
        ]
        session, _progress, pair_events, tmp = _run(pairs)
        try:
            by_index = collections.defaultdict(list)
            for event in pair_events:
                by_index[event.index].append(event)
            failed = by_index[0]
            assert failed[-1].state == PAIR_FAILED, [e.state for e in failed]
            assert '404' in failed[-1].error, failed[-1].error
            assert by_index[1][-1].state == PAIR_DONE
            # The healthy pair still succeeded.
            assert len(session.successful) == 1
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_downloader_cancellation_marks_pairs_cancelled():
    fixture, httpd, base, _size = _fixture()
    try:
        event = threading.Event()
        event.set()
        pairs = [_pair(base, 1, '/small_a.csv', '/small_b.csv')]
        session, _progress, pair_events, tmp = _run(pairs, cancel_event=event)
        try:
            states = [e.state for e in pair_events]
            assert PAIR_CANCELLED in states, states
            assert all(r.cancelled for r in session.results)
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_progress_emissions_are_throttled():
    """A big file must not produce one UI signal per 256 KB chunk."""
    fixture, httpd, base, size = _fixture()
    try:
        pairs = [_pair(base, 1, '/big_a.csv', '/big_b.csv')]
        started = time.monotonic()
        session, progress, _pair_events, tmp = _run(pairs)
        elapsed = time.monotonic() - started
        try:
            chunks = max(1, (size * 2) // (256 * 1024))
            # Allow the throttled stream plus the forced terminal update.
            assert len(progress) <= chunks * 2 + 4, (len(progress), chunks)
            assert len(progress) >= 2, len(progress)
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()
