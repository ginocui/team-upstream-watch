"""Fail-closed regression guard, not a security sandbox.

Tests exercise real source modules. Only fetch/command or the HTTPS transport
boundary is replaced by synthetic data. Child CLI invocations run this same
Python audit guard; arbitrary subprocesses and all socket audit events fail.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

CORE = Path(__file__).resolve().parent
PACKAGE = CORE.parents[1]
BLOCKED = []
_allowed_child = None
_expected_event = None
_installed = False


class OfflineViolation(RuntimeError):
    pass


def _audit(event, args):
    if event.startswith('socket.') or event in ('os.system', 'os.posix_spawn', 'os.spawn', 'os.exec'):
        _deny(event)
    if event == 'subprocess.Popen':
        executable, argv, cwd, env = args
        if (_allowed_child is None or executable != _allowed_child[0]
                or argv != _allowed_child[1] or cwd != _allowed_child[2]
                or env != _allowed_child[3]):
            _deny(event)


def _deny(event):
    BLOCKED.append({'event': event, 'expected': event == _expected_event})
    raise OfflineViolation('OFFLINE_GUARD_BLOCKED:' + event)


def install_guard():
    global _installed
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True


@contextmanager
def expected_denial(event):
    """Permit the guard's own denial tests to count their expected audit event."""
    global _expected_event
    if _expected_event is not None:
        raise RuntimeError('Nested expected denial is not supported')
    _expected_event = event
    start = len(BLOCKED)
    try:
        yield
    finally:
        _expected_event = None
        if [item['event'] for item in BLOCKED[start:]] != [event]:
            raise AssertionError('Expected exactly one guard denial for ' + event)


def tracker_cli(argv, *, capture_output=True, timeout=15):
    """Launch the real tracker CLI with the guard installed before tracker load."""
    global _allowed_child
    if not _installed:
        raise RuntimeError('Run via tests/core/run_tests.py to install the offline guard')
    args = [str(value) for value in argv]
    if not args or args[0] not in ('discover', 'tick', 'pending', 'ack', '--help'):
        raise ValueError('Unplanned tracker CLI action')
    command = [sys.executable, '-B', str(CORE / 'run_tests.py'), '--tracker-cli', *args]
    environment = os.environ.copy()
    environment['PYTHONDONTWRITEBYTECODE'] = '1'
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PYTHONUTF8'] = '1'
    audit_command = subprocess.list2cmdline(command) if os.name == 'nt' else command
    planned = (sys.executable, audit_command, str(PACKAGE), environment)
    if _allowed_child is not None:
        raise RuntimeError('Concurrent child execution is not supported')
    _allowed_child = planned
    try:
        return subprocess.run(command, executable=sys.executable, cwd=str(PACKAGE), env=environment,
                              stdin=subprocess.DEVNULL, capture_output=capture_output, timeout=timeout)
    finally:
        _allowed_child = None
