"""Contracts for the self-contained offline test entrypoint."""
from pathlib import Path
import unittest

from offline import OfflineViolation, expected_denial, tracker_cli


class OfflineRunnerTests(unittest.TestCase):
    def test_guarded_entrypoint_runs_real_tracker_cli(self):
        entry = Path(__file__).with_name('run_tests.py')
        self.assertTrue(entry.is_file(), 'Self-contained guarded test entrypoint is missing')
        result = tracker_cli(['--help'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b'{discover,tick,pending,ack}', result.stdout)

    def test_guard_denies_socket_creation_and_dns_before_real_network(self):
        import socket
        for event, operation in (('socket.__new__', socket.socket),
                                 ('socket.getaddrinfo', lambda: socket.getaddrinfo('example.invalid', 443))):
            with self.subTest(event=event), expected_denial(event):
                with self.assertRaisesRegex(OfflineViolation, 'OFFLINE_GUARD_BLOCKED:' + event):
                    operation()

    def test_guard_denies_unplanned_real_subprocess(self):
        import subprocess
        import sys
        with expected_denial('subprocess.Popen'):
            with self.assertRaisesRegex(OfflineViolation, 'OFFLINE_GUARD_BLOCKED:subprocess.Popen'):
                subprocess.run([sys.executable, '-B', '-c', 'raise SystemExit(91)'],
                               capture_output=True, timeout=5)


if __name__ == '__main__':
    unittest.main()
