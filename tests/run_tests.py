"""Test runner: executes every ``test_*`` function in this directory.

Usage:

    python tests/run_tests.py            # all suites
    python tests/run_tests.py core       # only test_core.py
    python tests/run_tests.py ui         # only test_ui.py

The runner is deliberately dependency-free (no pytest needed) so the project
can be verified with nothing but its runtime requirements installed. Every
test module is also pytest-compatible if pytest is available.
"""
import importlib
import sys
import time
import traceback
from pathlib import Path


TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

SUITES = {
    'core': 'test_core',
    'progress': 'test_progress',
    'downloader': 'test_downloader',
    'ui': 'test_ui',
    'canvas': 'test_pairing_canvas',
    'flow': 'test_flow',
    'scale': 'test_scale',
}


def _bootstrap_path():
    for path in (str(PROJECT_ROOT), str(TESTS_DIR)):
        if path not in sys.path:
            sys.path.insert(0, path)


def _run_module(module_name: str):
    module = importlib.import_module(module_name)
    tests = [
        (name, obj) for name, obj in vars(module).items()
        if name.startswith('test_') and callable(obj)
    ]
    tests.sort(key=lambda item: item[1].__code__.co_firstlineno)

    passed = failed = 0
    failures = []
    for name, func in tests:
        started = time.perf_counter()
        try:
            func()
        except Exception:
            failed += 1
            failures.append((name, traceback.format_exc()))
            print(f"  FAIL  {name}")
        else:
            passed += 1
            elapsed = (time.perf_counter() - started) * 1000
            print(f"  ok    {name}  ({elapsed:.0f} ms)")
    return passed, failed, failures


def main(argv):
    _bootstrap_path()
    selected = argv[1:] or list(SUITES)
    unknown = [name for name in selected if name not in SUITES]
    if unknown:
        print(f"Unknown suite(s): {', '.join(unknown)}")
        print(f"Available: {', '.join(SUITES)}")
        return 2

    total_passed = total_failed = 0
    all_failures = []
    for suite in selected:
        print(f"\n=== {suite} ({SUITES[suite]}) ===")
        passed, failed, failures = _run_module(SUITES[suite])
        total_passed += passed
        total_failed += failed
        all_failures.extend(failures)

    print(f"\n{'=' * 60}")
    print(f"RESULT: {total_passed} passed, {total_failed} failed")
    if all_failures:
        print(f"{'=' * 60}")
        for name, tb in all_failures:
            print(f"\n--- {name} ---")
            print(tb)
    return 0 if total_failed == 0 else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
