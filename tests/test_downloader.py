"""Downloader tests against a real local HTTP server.

Covers the behaviours that are easy to get wrong and expensive to discover in
production: format detection for extension-less endpoints, per-pair extension
matching, HTTP failures, URL de-duplication and cancellation.
"""
import collections
import functools
import http.server
import tempfile
import threading
from pathlib import Path

from core.downloader import BatchDownloader
from core.models import DownloadPair, DownloadSession, PairSource


class _Handler(http.server.SimpleHTTPRequestHandler):
    hits = collections.Counter()

    def do_GET(self):                                    # noqa: N802
        _Handler.hits[self.path] += 1
        return super().do_GET()

    def log_message(self, *args):
        pass


def _serve(directory: Path):
    handler = functools.partial(_Handler, directory=str(directory))
    httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return httpd


CSV_A = 'sku,price\n1,10\n2,20\n'
CSV_B = 'sku,price\n1,10\n2,25\n'
XML = '<?xml version="1.0"?><rss><channel><item><id>1</id></item></channel></rss>'


def _fixture():
    """Build a served directory and a pair factory bound to its base URL."""
    tmp = tempfile.TemporaryDirectory()
    served = Path(tmp.name) / 'served'
    served.mkdir()
    (served / 'a.csv').write_text(CSV_A, encoding='utf-8')
    (served / 'b.csv').write_text(CSV_B, encoding='utf-8')
    (served / 'feed.xml').write_text(XML, encoding='utf-8')
    # No extension: the format can only come from the payload.
    (served / 'noext').write_text(XML, encoding='utf-8')

    httpd = _serve(served)
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def make_pair(pair_id, left, right):
        return DownloadPair(
            pair_id=pair_id,
            left_name=left.rsplit('/', 1)[-1],
            left_url=f"{base}{left}",
            right_name=right.rsplit('/', 1)[-1],
            right_url=f"{base}{right}",
        )

    return tmp, httpd, base, make_pair


def _run(pairs, **kwargs):
    tmp = tempfile.TemporaryDirectory()
    session = DownloadSession(source=PairSource.MANUAL, pairs=list(pairs))
    downloader = BatchDownloader(**kwargs)
    downloader.run(session, Path(tmp.name))
    return session, tmp


def test_download_success():
    fixture, httpd, _base, make_pair = _fixture()
    try:
        session, tmp = _run([make_pair('p1', '/a.csv', '/b.csv')])
        try:
            assert len(session.successful) == 1, session.results
            result = session.successful[0]
            assert result.left_path.exists() and result.right_path.exists()
            # The downloader owns id generation and rewrites pair_id, so the
            # on-disk name is derived from that, not from the caller's id.
            assert result.pair.pair_id == 'pair_0001'
            assert result.left_path.name == result.right_path.name == 'pair_0001.csv'
            assert result.left_path.read_text(encoding='utf-8') == CSV_A
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_extension_sniffed_from_payload():
    """A URL without a suffix must still resolve to .xml from its content."""
    fixture, httpd, _base, make_pair = _fixture()
    try:
        session, tmp = _run([make_pair('p1', '/noext', '/noext')])
        try:
            assert len(session.successful) == 1, session.results
            assert session.successful[0].left_path.suffix == '.xml'
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_extension_mismatch_is_rejected():
    fixture, httpd, _base, make_pair = _fixture()
    try:
        session, tmp = _run([make_pair('p1', '/a.csv', '/feed.xml')])
        try:
            assert not session.successful
            assert 'Extension mismatch' in session.results[0].error
            assert not session.results[0].cancelled
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_http_error_is_reported():
    fixture, httpd, _base, make_pair = _fixture()
    try:
        session, tmp = _run([make_pair('p1', '/a.csv', '/missing.csv')])
        try:
            assert not session.successful
            assert 'HTTP 404' in session.results[0].error, session.results[0].error
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_identical_urls_downloaded_once():
    fixture, httpd, _base, make_pair = _fixture()
    _Handler.hits.clear()
    try:
        pairs = [
            make_pair('p1', '/a.csv', '/b.csv'),
            make_pair('p2', '/a.csv', '/b.csv'),
        ]
        session, tmp = _run(pairs)
        try:
            assert len(session.successful) == 2, session.results
            assert _Handler.hits['/a.csv'] == 1, dict(_Handler.hits)
            assert _Handler.hits['/b.csv'] == 1, dict(_Handler.hits)
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_cancellation_marks_results_cancelled():
    fixture, httpd, _base, make_pair = _fixture()
    try:
        event = threading.Event()
        event.set()
        session, tmp = _run(
            [make_pair('p1', '/a.csv', '/b.csv')], cancel_event=event,
        )
        try:
            assert not session.successful
            assert session.results and all(r.cancelled for r in session.results)
            # Cancelled pairs must not be reported as failures.
            assert session.failed == []
            assert len(session.cancelled) == 1
        finally:
            session.cleanup()
            tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()


def test_cleanup_removes_run_directory():
    fixture, httpd, _base, make_pair = _fixture()
    try:
        session, tmp = _run([make_pair('p1', '/a.csv', '/b.csv')])
        run_dir = session.base_dir
        assert run_dir.exists()
        session.cleanup()
        assert not run_dir.exists()
        # Idempotent.
        session.cleanup()
        tmp.cleanup()
    finally:
        httpd.shutdown()
        fixture.cleanup()
