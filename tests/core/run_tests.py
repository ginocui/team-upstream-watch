"""Self-contained stdlib tests with socket/unplanned-child audit rejection.

This is an accidental-I/O regression guard, NOT an adversarial security sandbox.
No original scheduler, native delivery, real config, or runtime DB is imported.
Run: python -B tests/core/run_tests.py
"""
import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import tempfile
import unittest

from offline import BLOCKED, CORE, PACKAGE, install_guard

install_guard()
sys.dont_write_bytecode = True
sys.path[:0] = [str(CORE), str(CORE / 'reference_provider'), str(PACKAGE / 'scripts')]


def unexpected_denials():
    return [entry for entry in BLOCKED if not entry['expected']]


def configure_temporary_root():
    configured = os.environ.get('TEAM_WATCH_TEST_ROOT')
    root = Path(configured).resolve() if configured else PACKAGE.parent / 'test-temp/core'
    if not root.is_absolute() or (configured and not Path(configured).is_absolute()):
        raise ValueError('TEAM_WATCH_TEST_ROOT must be absolute')
    root.mkdir(parents=True, exist_ok=True)
    os.environ['TEAM_WATCH_TEST_ROOT'] = str(root)
    for name in ('TMP', 'TEMP', 'TMPDIR'):
        os.environ[name] = str(root)
    tempfile.tempdir = str(root)


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ['--tracker-cli']:
        configure_temporary_root()
        sys.argv = [str(PACKAGE / 'scripts/reference_tracker.py'), *args[1:]]
        try:
            runpy.run_path(sys.argv[0], run_name='__main__')
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        else:
            code = 0
        if unexpected_denials():
            print('OFFLINE_GUARD_UNEXPECTED_IO', file=sys.stderr)
            return 1
        return code
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('tests', nargs='*', help='Optional unittest test identifiers')
    parser.add_argument('--summary-json', type=Path)
    parsed = parser.parse_args(args)
    configure_temporary_root()
    loader = unittest.TestLoader()
    suite = (loader.loadTestsFromNames(parsed.tests) if parsed.tests
             else loader.discover(str(CORE), pattern='test_*.py'))
    selected = list(cases(suite))
    identifiers = [case.id() for case in selected]
    if any('scheduler_adapter' in name or 'reference_review' in name for name in identifiers):
        raise RuntimeError('Deployment-specific test selected')
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    summary = {'python': sys.version.split()[0], 'platform': sys.platform,
               'tests_run': result.testsRun, 'selected_count': len(identifiers),
               'failures': len(result.failures), 'errors': len(result.errors),
               'skipped': len(result.skipped), 'guard_denials': BLOCKED,
               'unexpected_guard_denials': unexpected_denials(), 'test_ids': identifiers,
               'success': result.wasSuccessful() and not unexpected_denials(),
               'guard_scope': 'Python audit regression guard, not a security sandbox'}
    if parsed.summary_json:
        with parsed.summary_json.open('x', encoding='utf-8') as stream:
            json.dump(summary, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print('OFFLINE_RESULT ' + json.dumps({key: value for key, value in summary.items() if key != 'test_ids'}, sort_keys=True))
    return 0 if summary['success'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
