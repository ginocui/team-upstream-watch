"""Owned synthetic discovery fixtures; no network or real resources."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'scripts/reference_tracker.py'


class Discovery(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ['TEAM_WATCH_TEST_ROOT'])
        self.case = Path(tempfile.mkdtemp(prefix='reference-tracker-', dir=base))
        self.sources = self.case / 'shared-technical'
        self.sources.mkdir()

    def tracker(self):
        self.assertTrue(MODULE.is_file(), 'automatic reference discovery capability is not implemented')
        spec = importlib.util.spec_from_file_location('tracker', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_dynamic_document_discovery_and_new_reference_are_deduplicated(self):
        (self.sources / 'first.md').write_text('参考 https://github.com/Owner/First/tree/main/docs\n同源 https://github.com/owner/first\n', encoding='utf-8')
        tracker = self.tracker()
        first = tracker.discover([self.sources])
        self.assertEqual([r['key'] for r in first['repositories']], ['owner/first'])
        self.assertEqual(len(first['repositories'][0]['evidence']), 2)
        (self.sources / 'new-reference.md').write_text('新增 https://github.com/Owner/Second\n', encoding='utf-8')
        second = tracker.discover([self.sources])
        self.assertEqual([r['key'] for r in second['repositories']], ['owner/first', 'owner/second'])
        self.assertEqual(second['coverage']['files_read'], 2)
        self.assertTrue(second['coverage']['complete'])
        self.assertEqual(second['repositories'][0]['evidence'][0]['line'], 1)
        self.assertFalse((self.case / 'state').exists())


    def test_excluded_private_trees_links_and_size_failures_are_explicit(self):
        (self.sources / 'public.md').write_text('https://github.com/owner/public\nhttps://github.com.evil.invalid/owner/not-a-repo\n', encoding='utf-8')
        private = self.sources / 'private'
        private.mkdir()
        (private / 'secret.md').write_text('https://github.com/private/never-read', encoding='utf-8')
        hidden = self.sources / '.git'
        hidden.mkdir()
        (hidden / 'cache.md').write_text('https://github.com/private/not-read', encoding='utf-8')
        (self.sources / 'oversize.md').write_bytes(b'x' * 200)
        tracker = self.tracker()
        self.assertIn('max_file_bytes', tracker.discover.__code__.co_varnames, 'discovery lacks bounded safe traversal')
        result = tracker.discover([self.sources], max_file_bytes=150)
        self.assertEqual([r['key'] for r in result['repositories']], ['owner/public'])
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(any(row['reason'] == 'FILE_SIZE_LIMIT' for row in result['issues']))
        self.assertEqual(result['coverage']['files_read'], 1)
        self.assertEqual(result['coverage']['excluded_directories'], 2)
        absent = tracker.discover([self.sources / 'missing'])
        self.assertFalse(absent['coverage']['complete'])
        self.assertEqual(absent['issues'][0]['reason'], 'ROOT_UNAVAILABLE')


    def test_seed_aliases_keep_reviewed_baseline_and_new_reference_has_no_adoption(self):
        (self.sources / 'refs.json').write_text(json.dumps({'urls': ['https://github.com/old/name', 'https://github.com/new/name', 'https://github.com/new/project']}), encoding='utf-8')
        tracker = self.tracker()
        self.assertTrue(callable(getattr(tracker, 'reconcile', None)), 'missing reference registry reconciliation')
        seed = [{'repository': 'https://github.com/new/name', 'aliases': ['https://github.com/old/name'],
                 'baseline_commit': 'a' * 40, 'usage': 'adapted_code', 'consumer': 'synthetic-project', 'interval_hours': 6}]
        rows = tracker.reconcile(tracker.discover([self.sources]), seed, {})
        self.assertEqual(sorted(rows), ['new/name', 'new/project'])
        self.assertEqual(rows['new/name']['baseline_commit'], 'a' * 40)
        self.assertEqual(len(rows['new/name']['evidence']), 2)
        self.assertIsNone(rows['new/project']['baseline_commit'])
        self.assertFalse(rows['new/project']['adoption_changed'])
        rows['new/name']['last_observation'] = {'commit': 'b' * 40}
        again = tracker.reconcile(tracker.discover([self.sources]), seed, rows)
        self.assertEqual(again['new/name']['baseline_commit'], 'a' * 40)
        self.assertEqual(again['new/name']['last_observation']['commit'], 'b' * 40)
        (self.sources / 'refs.json').unlink()
        removed = tracker.reconcile(tracker.discover([self.sources]), seed, rows)
        self.assertIn('new/project', removed)
        self.assertEqual(removed['new/project']['discovery_status'], 'NO_LONGER_REFERENCED')


    def test_batch_due_checks_preserve_baseline_and_dedupe_changes_with_unknown_recovery(self):
        tracker = self.tracker()
        self.assertTrue(callable(getattr(tracker, 'check_due', None)), 'missing due batch observation and incremental events')
        row = {'key': 'owner/first', 'repository': 'https://github.com/owner/first', 'baseline_commit': 'a' * 40,
               'discovery_status': 'REFERENCED', 'interval_hours': 6, 'last_observation': None,
               'adoption_changed': False, 'evidence': [], 'usage': 'research', 'consumer': None, 'aliases': []}
        calls = []
        def observe(repository, previous):
            calls.append((repository, previous))
            return {'status': 'OK', 'snapshot': {'repository': repository, 'head_sha': 'b' * 40}, 'problems': []}
        checked, events = tracker.check_due({'owner/first': row}, 1000, observe)
        self.assertEqual(len(calls), 1)
        self.assertEqual(events[0]['event'], 'FIRST_OBSERVATION')
        self.assertEqual(checked['owner/first']['baseline_commit'], 'a' * 40)
        same, quiet = tracker.check_due(checked, 1001, observe)
        self.assertFalse(quiet)
        self.assertEqual(len(calls), 1)
        same, quiet = tracker.check_due(same, 30000, observe)
        self.assertFalse(quiet)
        self.assertEqual(len(calls), 2)
        def broken(repository, previous):
            raise TimeoutError('synthetic')
        unknown, failed = tracker.check_due(same, 60000, broken)
        self.assertEqual(failed[0]['event'], 'OBSERVATION_UNKNOWN')
        self.assertEqual(unknown['owner/first']['last_good_snapshot']['head_sha'], 'b' * 40)
        self.assertEqual(unknown['owner/first']['baseline_commit'], 'a' * 40)
        repeated, quiet = tracker.check_due(unknown, 90000, broken)
        self.assertEqual(quiet, [])
        recovered, events = tracker.check_due(repeated, 120000, observe)
        self.assertEqual(events[0]['event'], 'OBSERVATION_RECOVERED')
        self.assertFalse(recovered['owner/first']['adoption_changed'])
        self.assertEqual(row['last_observation'], None)


    def test_transactional_runner_autoincludes_new_refs_and_preserves_evidence_on_failure(self):
        tracker = self.tracker()
        self.assertTrue(callable(getattr(tracker, 'run_tracking', None)), 'missing durable tracking execution')
        (self.sources / 'first.md').write_text('https://github.com/owner/first', encoding='utf-8')
        state = self.case / 'runtime'
        config = {'discovery_roots': [str(self.sources)], 'seeds': [], 'watches': [], 'state_root': str(state), 'max_checks': 3}
        def observe(repo, previous):
            return {'status': 'OK', 'snapshot': {'repository': repo, 'head_sha': 'a' * 40}, 'problems': []}
        result = tracker.run_tracking(config, now=1000, observe=observe)
        self.assertEqual(result['counts']['referenced_repositories'], 1)
        self.assertTrue((state / 'tracking.sqlite3').is_file())
        (self.sources / 'next.md').write_text('https://github.com/owner/next', encoding='utf-8')
        result = tracker.run_tracking(config, now=1001, observe=observe)
        self.assertEqual(result['counts']['referenced_repositories'], 2)
        self.assertEqual([e['key'] for e in result['events']], ['owner/next'])
        quiet = tracker.run_tracking(config, now=1002, observe=observe)
        self.assertEqual(quiet['events'], [])
        self.assertFalse(quiet['adoption_changed'])
        import sqlite3
        conn = sqlite3.connect(state / 'tracking.sqlite3')
        conn.execute('BEGIN IMMEDIATE')
        with self.assertRaisesRegex(RuntimeError, 'TRACKING_BUSY'):
            tracker.run_tracking(config, now=1003, observe=observe)
        conn.rollback()
        conn.close()
        self.assertEqual(len(list((state / 'runs').glob('*.json'))), 3)
        with self.assertRaisesRegex(ValueError, 'STATE_INSIDE_INPUT'):
            tracker.run_tracking({**config, 'state_root': str(self.sources / 'state')}, now=1004, observe=observe)


    def test_discovery_carries_versioned_registry_baselines_and_aliases_without_manual_seeding(self):
        tracker = self.tracker()
        data = {'schema': 'we_external_reference/1', 'maintainer': 'SYNTHETIC',
                'baseline': {'repository': 'https://github.com/owner/first', 'commit': 'a' * 40,
                             'ref': 'refs/heads/main', 'observed_at': '2026-01-01T00:00:00Z', 'evidence_ref': 'synthetic'}}
        (self.sources / 'registry.json').write_text(json.dumps(data), encoding='utf-8')
        found = tracker.discover([self.sources])
        self.assertIn('declared_seeds', found, 'new structured registrations do not supply review baseline')
        rows = tracker.reconcile(found, [], {})
        self.assertEqual(rows['owner/first']['baseline_commit'], 'a' * 40)
        data['baseline']['commit'] = 'b' * 40
        (self.sources / 'registry.json').write_text(json.dumps(data), encoding='utf-8')
        changed = tracker.reconcile(tracker.discover([self.sources]), [], rows)
        self.assertEqual(changed['owner/first']['baseline_commit'], 'a' * 40)
        self.assertTrue(changed['owner/first']['baseline_review_pending'])
        self.assertEqual(changed['owner/first']['declared_baseline_commit'], 'b' * 40)
        batch = {'schema': 'we.external-reference-batch.v1', 'repositories': [
            {'resolved_url': 'https://github.com/owner/renamed', 'original_url': 'https://github.com/owner/old',
             'reviewed_sha': 'c' * 40, 'disposition': 'METHOD_REFERENCE_NO_ROUTINE_WATCH'}]}
        (self.sources / 'batch.json').write_text(json.dumps(batch), encoding='utf-8')
        batch_rows = tracker.reconcile(tracker.discover([self.sources]), [], changed)
        self.assertIn('owner/renamed', batch_rows)
        self.assertNotIn('owner/old', batch_rows)
        self.assertEqual(batch_rows['owner/renamed']['baseline_commit'], 'c' * 40)


    def test_explicit_watch_due_scope_is_persisted_independently_of_repo_and_schedule(self):
        tracker = self.tracker()
        self.assertIn('observe_watch', tracker.run_tracking.__code__.co_varnames, 'missing original specialized watch duties')
        config = {'discovery_roots': [str(self.sources)], 'seeds': [], 'state_root': str(self.case / 'state'),
                  'max_checks': 1, 'watches': [{'id': 'old-hourly-pr', 'repository': 'https://github.com/owner/first',
                   'kind': 'pull', 'number': 10, 'interval_hours': 1, 'consumer': 'original-maintainer', 'delivery': 'local'}]}
        calls = []
        def observer(watch, previous):
            calls.append(watch['number'])
            return {'status': 'OK', 'snapshot': {'number': watch['number'], 'head_sha': 'a' * 40}, 'problems': []}
        one = tracker.run_tracking(config, now=1000, observe=lambda repo, previous: {}, observe_watch=observer)
        self.assertEqual(calls, [10])
        self.assertEqual(one['watches']['old-hourly-pr']['last_observation']['status'], 'OK')
        two = tracker.run_tracking(config, now=1001, observe=lambda repo, previous: {}, observe_watch=observer)
        self.assertEqual(calls, [10])
        self.assertEqual(two['events'], [])
        three = tracker.run_tracking(config, now=5000, observe=lambda repo, previous: {}, observe_watch=observer)
        self.assertEqual(calls, [10, 10])
        self.assertEqual(three['events'], [])
        self.assertFalse(three['adoption_changed'])


    def test_weekly_slots_do_not_drift_and_watch_identity_change_cannot_reuse_old_receipt(self):
        tracker = self.tracker()
        self.assertTrue(callable(getattr(tracker, 'is_due', None)), 'missing calendar-aware due rules')
        from datetime import datetime
        monday = datetime.fromisoformat('2026-10-19T09:00:00+08:00').timestamp()
        row = {'schedule': {'kind': 'weekly', 'weekday': 0, 'hour': 9, 'minute': 0}, 'checked_at': monday - 100}
        self.assertTrue(tracker.is_due(row, monday))
        row['checked_at'] = monday + 5
        self.assertFalse(tracker.is_due(row, monday + 100))
        self.assertTrue(tracker.is_due(row, monday + 7 * 86400))
        config = {'discovery_roots': [str(self.sources)], 'seeds': [], 'state_root': str(self.case / 'calendar'),
                  'watches': [{'id': 'same', 'repository': 'https://github.com/owner/first', 'kind': 'pull', 'number': 1, 'interval_hours': 24}]}
        calls = []
        def observer(watch, previous):
            calls.append((watch['number'], previous))
            return {'status': 'OK', 'snapshot': {'number': watch['number']}, 'problems': []}
        tracker.run_tracking(config, now=monday, observe=lambda a, b: {}, observe_watch=observer)
        config['watches'][0]['number'] = 2
        report = tracker.run_tracking(config, now=monday + 1, observe=lambda a, b: {}, observe_watch=observer)
        self.assertEqual([c[0] for c in calls], [1, 2])
        self.assertIsNone(calls[-1][1])
        self.assertEqual(report['watches']['same']['last_good_snapshot']['number'], 2)


    def test_persistent_event_outbox_and_compact_summary_do_not_drop_local_reviews(self):
        tracker = self.tracker()
        self.assertTrue(callable(getattr(tracker, 'pending_reviews', None)), 'missing persistent change outbox')
        (self.sources / 'source.md').write_text('https://github.com/owner/first', encoding='utf-8')
        config = {'discovery_roots': [str(self.sources)], 'seeds': [], 'watches': [], 'state_root': str(self.case / 'outbox')}
        def observe(repo, previous):
            return {'status': 'OK', 'snapshot': {'repository': repo, 'head_sha': 'a' * 40}, 'problems': []}
        first = tracker.run_tracking(config, now=1000, observe=observe)
        pending = tracker.pending_reviews(config)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['event']['event'], 'FIRST_OBSERVATION')
        tracker.run_tracking(config, now=1001, observe=observe)
        self.assertEqual(len(tracker.pending_reviews(config)), 1)
        tracker.acknowledge_review(config, pending[0]['id'], 'synthetic-receiver-receipt')
        self.assertEqual(tracker.pending_reviews(config), [])
        self.assertEqual(tracker.render_summary({'counts': {}, 'events': []}), '')
        report = {'counts': {'referenced_repositories': 1}, 'events': [
            {'event': 'UPSTREAM_CHANGED', 'repository': 'https://github.com/private/local', 'delivery': 'local', 'status': 'OK'},
            {'event': 'UPSTREAM_CHANGED', 'repository': 'https://github.com/owner/public', 'status': 'OK'}]}
        summary = tracker.render_summary(report)
        self.assertNotIn('private/local', summary)
        self.assertIn('owner/public', summary)
        self.assertLess(len(summary), 3500)
        self.assertEqual(first['counts']['events'], 1)


    def test_partial_observation_does_not_repeatedly_alert_on_transient_compare_metadata(self):
        tracker = self.tracker()
        self.assertTrue(callable(getattr(tracker, 'observation_identity', None)), 'missing stable observation change identity')
        base = {'status': 'OK', 'snapshot': {'repository': 'https://github.com/owner/repo', 'head_sha': 'a' * 40,
                 'comparison': {'changed_paths': ['first.py'], 'from_sha': 'b' * 40}}, 'problems': []}
        later = {'status': 'OK', 'snapshot': {'repository': 'https://github.com/owner/repo', 'head_sha': 'a' * 40,
                 'comparison': None}, 'problems': []}
        self.assertEqual(tracker.observation_identity(base), tracker.observation_identity(later))
        row = {'key': 'owner/repo', 'repository': 'https://github.com/owner/repo', 'baseline_commit': 'b' * 40,
               'last_observation': None, 'interval_hours': 1, 'discovery_status': 'REFERENCED'}
        first, _ = tracker.check_due({'owner/repo': row}, 1000, lambda a, b: base)
        second, events = tracker.check_due(first, 5000, lambda a, b: later)
        self.assertEqual(events, [])
        partial = {'status': 'PARTIAL', 'snapshot': {'head_sha': 'c' * 40}, 'problems': [{'code': 'PAGE_LIMIT'}]}
        third, changes = tracker.check_due(second, 9000, lambda a, b: partial)
        self.assertEqual(changes[0]['event'], 'OBSERVATION_PARTIAL')
        self.assertEqual(third['owner/repo']['last_good_snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(third['owner/repo']['baseline_commit'], 'b' * 40)


    def test_public_url_forms_scoped_registry_depth_and_huggingface_are_discovered(self):
        tracker = self.tracker()
        self.assertIn('scopes', tracker.discover.__code__.co_varnames, 'missing dynamic permitted-source filters')
        provenance = self.sources / 'provenance'
        (provenance / 'a' / 'old-sha').mkdir(parents=True)
        (provenance / 'a' / 'registry.json').write_text('{"source":"https://github.com/owner/allowed"}', encoding='utf-8')
        (provenance / 'a' / 'old-sha' / 'private.json').write_text('{"source":"https://github.com/private/no"}', encoding='utf-8')
        (self.sources / 'published.md').write_text('https://raw.githubusercontent.com/owner/raw/main/a.py\nhttps://api.github.com/repos/owner/api\nhttps://huggingface.co/owner/model/tree/main\n', encoding='utf-8')
        scopes = [{'path': str(provenance), 'include': ['*/registry.json', '*/review.json'], 'max_depth': 1},
                  {'path': str(self.sources), 'include': ['published.md'], 'max_depth': 0}]
        found = tracker.discover([], scopes=scopes)
        keys = {r['key'] for r in found['repositories']}
        self.assertEqual(keys, {'owner/allowed', 'owner/raw', 'owner/api', 'hf:owner/model'})
        self.assertEqual(tracker.repository_key('https://huggingface.co/owner/model'), 'hf:owner/model')
        self.assertEqual(found['coverage']['files_read'], 2)
        rows = tracker.reconcile(found, [], {})
        self.assertIn('hf:owner/model', rows)


    def test_cli_discovery_is_offline_and_tick_requires_explicit_network_permission(self):
        from offline import tracker_cli
        (self.sources / 'ref.md').write_text('https://github.com/owner/repo', encoding='utf-8')
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'runtime'), 'seeds': [], 'watches': []}
        path = self.case / 'config.json'
        path.write_text(json.dumps(config), encoding='utf-8')
        argv = ['discover', '--config', str(path)]
        proc = tracker_cli(argv, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout, 'missing CLI discovery result')
        data = json.loads(proc.stdout)
        self.assertEqual(data['repositories'][0]['key'], 'owner/repo')
        self.assertFalse(Path(config['state_root']).exists())
        rejected = tracker_cli(['tick', '--config', str(path)], capture_output=True, timeout=15)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn(b'PUBLIC_NETWORK_NOT_ENABLED', rejected.stderr)
        self.assertFalse(Path(config['state_root']).exists())


    def test_outbox_database_connections_are_closed_after_read_and_ack(self):
        import sqlite3
        from unittest.mock import patch
        tracker = self.tracker()
        (self.sources / 'source.md').write_text('https://github.com/owner/repo', encoding='utf-8')
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'close'), 'watches': []}
        tracker.run_tracking(config, now=1000, observe=lambda a, b: {'status': 'OK', 'snapshot': {}, 'problems': []})
        opened = []
        class Connection(sqlite3.Connection):
            was_closed = False
            def close(self):
                self.was_closed = True
                super().close()
        native = sqlite3.connect
        def connect(*args, **kwargs):
            connection = native(*args, factory=Connection, **kwargs)
            opened.append(connection)
            return connection
        with patch.object(tracker.sqlite3, 'connect', side_effect=connect):
            pending = tracker.pending_reviews(config)
            tracker.acknowledge_review(config, pending[0]['id'], 'receipt')
        self.assertEqual(len(opened), 2)
        self.assertTrue(all(connection.was_closed for connection in opened), 'outbox leaks sqlite handles')


    def test_database_path_links_and_partial_input_cannot_escape_or_prune_history(self):
        from unittest.mock import patch
        tracker = self.tracker()
        (self.sources / 'ref.md').write_text('https://github.com/owner/repo', encoding='utf-8')
        state = self.case / 'protected'
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(state), 'watches': []}
        observe = lambda a, b: {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
        tracker.run_tracking(config, now=1000, observe=observe)
        native = tracker._reparse
        with patch.object(tracker, '_reparse', side_effect=lambda p: Path(p).name == 'tracking.sqlite3' or native(p)):
            with self.assertRaisesRegex(ValueError, 'STATE_REPARSE'):
                tracker.run_tracking(config, now=1001, observe=observe)
        with self.assertRaisesRegex(ValueError, 'STATE_INSIDE_INPUT'):
            tracker.run_tracking({**config, 'discovery_roots': [], 'discovery_scopes': [{'path': str(state.parent)}]}, now=1002, observe=observe)
        discovered = tracker.discover([self.sources / 'missing'])
        previous = {'owner/repo': {'key': 'owner/repo', 'evidence': [{'path': 'previous-source'}], 'baseline_commit': 'a' * 40}}
        result = tracker.reconcile(discovered, [], previous)
        self.assertEqual(result['owner/repo']['discovery_status'], 'DISCOVERY_UNKNOWN')
        self.assertEqual(result['owner/repo']['evidence'], previous['owner/repo']['evidence'])


    def test_discovery_package_declarations_and_unsupported_hosts_are_not_silently_lost(self):
        tracker = self.tracker()
        (self.sources / 'requirements.txt').write_text('markdown-it-py==4.2.0\nmdurl==0.1.2\n# comment\n', encoding='utf-8')
        (self.sources / 'other.md').write_text('https://gitlab.com/owner/project\nhttps://codeberg.org/owner/library\n', encoding='utf-8')
        result = tracker.discover([self.sources])
        self.assertIn('unresolved_dependencies', result, 'unmapped package/source references are dropped')
        self.assertEqual([r['declaration'] for r in result['unresolved_dependencies']], ['markdown-it-py==4.2.0', 'mdurl==0.1.2'])
        self.assertEqual(sorted(r['url'] for r in result['unsupported_sources']), ['https://codeberg.org/owner/library', 'https://gitlab.com/owner/project'])
        self.assertFalse(result['coverage']['upstream_support_complete'])


    def test_due_budget_does_not_starve_existing_references_and_failed_calls_are_spaced(self):
        tracker = self.tracker()
        self.assertIn('max_seconds', tracker.check_due.__code__.co_varnames, 'missing per-tick work bound')
        rows = {str(i): {'key': str(i), 'repository': 'https://github.com/owner/repo' + str(i),
                        'discovery_status': 'REFERENCED', 'checked_at': 1, 'interval_hours': 1} for i in range(3)}
        clock = [0]
        calls = []
        def observe(repo, previous):
            calls.append(repo)
            clock[0] += 5
            return {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
        checked, events = tracker.check_due(rows, 10000, observe, max_seconds=3, clock=lambda: clock[0])
        self.assertEqual(len(calls), 1)
        self.assertEqual(sum(r.get('checked_at') == 10000 for r in checked.values()), 1)
        checked, events = tracker.check_due(checked, 10001, observe, max_seconds=3, clock=lambda: clock[0])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1])
        self.assertEqual(len(events), 1)


    def test_run_tracking_uses_explicit_adapters_and_suppresses_duplicate_repository_polling(self):
        from unittest.mock import patch
        import sys
        import types
        tracker = self.tracker()
        (self.sources / 'public.md').write_text('https://github.com/owner/anatomy', encoding='utf-8')
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'adapter'), 'seeds': [],
                  'watches': [{'id': 'atom', 'repository': 'https://github.com/owner/anatomy', 'kind': 'atom', 'branch': 'main', 'interval_hours': 168}]}
        calls = []
        def default(repo, previous):
            calls.append('unexpected repository')
            return {'status': 'OK', 'snapshot': {}, 'problems': []}
        def specialized(watch, previous):
            calls.append(watch['kind'])
            return {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
        report = tracker.run_tracking(config, now=1000, observe=default, observe_watch=specialized)
        self.assertEqual(calls, ['atom'], 'specialized reference is also polled through ordinary API')
        self.assertEqual(report['repositories']['owner/anatomy']['observation_source'], 'watch:atom')


    def test_received_provider_compare_does_not_create_a_false_second_change(self):
        tracker = self.tracker()
        first = {'status': 'OK', 'snapshot': {'repository': 'https://github.com/owner/repo', 'head_sha': 'a' * 40,
                 'coverage': {'head': 'OK', 'compare': 'OK'}, 'compare': {'base_sha': 'b' * 40, 'head_sha': 'a' * 40}}, 'problems': []}
        unchanged = {'status': 'OK', 'snapshot': {'repository': 'https://github.com/owner/repo', 'head_sha': 'a' * 40,
                 'coverage': {'head': 'OK', 'compare': 'NOT_REQUESTED'}, 'compare': None}, 'problems': []}
        self.assertEqual(tracker.observation_identity(first), tracker.observation_identity(unchanged),
                         'received provider uses compare, not comparison; one upstream update becomes two alerts')


    def test_all_watches_share_the_tick_budget_and_oldest_due_runs_first(self):
        from unittest.mock import patch
        tracker = self.tracker()
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'shared-budget'),
                  'max_checks': 1, 'max_seconds': 3, 'watches': [
                   {'id': 'a', 'repository': 'https://github.com/owner/first', 'kind': 'pull', 'number': 1, 'interval_hours': 1},
                   {'id': 'b', 'repository': 'https://github.com/owner/second', 'kind': 'pull', 'number': 2, 'interval_hours': 1}]}
        ticks = [0]
        calls = []
        def observer(watch, previous):
            calls.append(watch['id'])
            ticks[0] += 5
            return {'status': 'OK', 'snapshot': {'number': watch['number']}, 'problems': []}
        with patch.object(tracker.time, 'monotonic', side_effect=lambda: ticks[0]):
            tracker.run_tracking(config, now=1000, observe=lambda a, b: {}, observe_watch=observer)
            self.assertEqual(calls, ['a'], 'first tick exceeded the shared source budget')
            tracker.run_tracking(config, now=1001, observe=lambda a, b: {}, observe_watch=observer)
        self.assertEqual(calls, ['a', 'b'], 'watch loop resets the time and count budget for every source')


    def test_new_project_package_manifests_are_registered_without_github_urls(self):
        tracker = self.tracker()
        (self.sources / 'pyproject.toml').write_text('[project]\nname="synthetic"\ndependencies=["sample-lib>=1"]\n', encoding='utf-8')
        (self.sources / 'package.json').write_text(json.dumps({'dependencies': {'example-lib': '^2.0'}}), encoding='utf-8')
        (self.sources / 'Cargo.toml').write_text('[package]\nname="synthetic"\n[dependencies]\nexample-crate="3"\n', encoding='utf-8')
        (self.sources / 'go.mod').write_text('module example.invalid/synthetic\nrequire example.invalid/library v1.2.3\n', encoding='utf-8')
        found = tracker.discover([self.sources])
        declarations = {row['declaration'] for row in found['unresolved_dependencies']}
        self.assertEqual(declarations, {'sample-lib>=1', 'example-lib@^2.0', 'example-crate@3', 'example.invalid/library v1.2.3'})
        self.assertEqual(found['coverage']['files_read'], 4)
        self.assertFalse(found['coverage']['upstream_support_complete'])


    def test_source_discovery_does_not_read_frozen_versions_or_linked_files(self):
        from unittest.mock import patch
        tracker = self.tracker()
        old = self.sources / 'versions' / 'v1'
        old.mkdir(parents=True)
        (old / 'old.md').write_text('https://github.com/owner/frozen', encoding='utf-8')
        public = self.sources / 'public.md'
        public.write_text('https://github.com/owner/current', encoding='utf-8')
        linked = self.sources / 'linked.md'
        linked.write_text('https://github.com/owner/hidden', encoding='utf-8')
        native = tracker.Path.lstat
        import types
        def lstat(path, *args, **kwargs):
            info = native(path, *args, **kwargs)
            if path.name == 'linked.md':
                return types.SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0, st_nlink=2)
            return info
        with patch.object(tracker.Path, 'lstat', lstat):
            found = tracker.discover([self.sources])
        self.assertEqual([r['key'] for r in found['repositories']], ['owner/current'])
        self.assertTrue(any(r['reason'] == 'HARDLINK_NOT_READ' for r in found['issues']))


    def test_still_scheduled_legacy_sources_are_held_without_losing_new_discovery(self):
        tracker = self.tracker()
        (self.sources / 'refs.md').write_text('https://github.com/owner/legacy\nhttps://github.com/owner/new', encoding='utf-8')
        calls = []
        def observer(repo, previous):
            calls.append(repo)
            return {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'migration-hold'),
                  'observation_holds': ['https://github.com/owner/legacy'], 'watches': [
                      {'id': 'old', 'repository': 'https://github.com/owner/legacy', 'kind': 'pull', 'number': 1,
                       'migration_hold': True}]}
        result = tracker.run_tracking(config, now=1000, observe=observer, observe_watch=lambda a, b: self.fail('held watch must not run'))
        self.assertEqual(calls, ['https://github.com/owner/new'])
        self.assertEqual(result['counts']['referenced_repositories'], 2)
        self.assertIsNone(result['repositories']['owner/legacy']['last_observation'])
        self.assertTrue(result['repositories']['owner/legacy']['migration_hold'])
        self.assertTrue(result['watches']['old']['migration_hold'])




    def test_rate_limited_repository_keeps_public_fallback_as_partial_not_no_update(self):
        from unittest.mock import patch
        import sys
        import github_reference_source
        import reference_watch_sources
        tracker = self.tracker()
        repo = 'https://github.com/owner/repo'
        limited = {'status': 'UNKNOWN', 'snapshot': {'repository': repo, 'coverage': {'metadata': 'UNKNOWN'}},
                   'problems': [{'code': 'HTTP_403', 'scope': 'metadata', 'rate_limit': {'remaining': 0, 'reset': 2000}}]}
        fallback = {'status': 'OK', 'snapshot': {'repository': repo, 'head_sha': 'a' * 40,
                    'releases': [], 'coverage': {'head': 'OK', 'release_feed': 'OK'}}, 'problems': []}
        with patch.object(github_reference_source, 'observe_repository', return_value=limited), \
             patch.object(reference_watch_sources, 'observe_public_fallback', return_value=fallback, create=True):
            result = tracker.observe_source(repo)
        self.assertEqual(result['status'], 'PARTIAL', 'known public refs/feeds were lost behind API exhaustion')
        self.assertEqual(result['snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(result['snapshot']['coverage']['api_metadata'], 'UNKNOWN')
        self.assertEqual(result['snapshot']['observation_route'], 'PUBLIC_GIT_HEAD_AND_RELEASE_ATOM')
        self.assertIn('API_COVERAGE_UNAVAILABLE', [p['code'] for p in result['problems']])


    def test_verified_package_source_mapping_registers_repo_without_changing_adoption(self):
        tracker = self.tracker()
        (self.sources / 'requirements.txt').write_text('sample-lib==1.2\n', encoding='utf-8')
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'package-sources'),
                  'package_sources': {'sample-lib': {'repository': 'https://github.com/owner/library',
                    'evidence': {'url': 'https://pypi.org/pypi/sample-lib/json', 'observed_at': 'SYNTHETIC'},
                    'consumer': 'synthetic-project'}}, 'watches': []}
        calls = []
        def observer(repo, previous):
            calls.append(repo)
            return {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
        found = tracker.run_tracking(config, now=1000, observe=observer)
        self.assertEqual(calls, ['https://github.com/owner/library'])
        self.assertEqual(found['unresolved_dependencies'], [])
        self.assertEqual(found['resolved_dependencies'][0]['declaration'], 'sample-lib==1.2')
        self.assertIsNone(found['repositories']['owner/library']['baseline_commit'])
        self.assertFalse(found['repositories']['owner/library']['adoption_changed'])


    def test_accepted_local_consumer_view_contains_only_its_watch_and_durable_pending_events(self):
        tracker = self.tracker()
        state = self.case / 'runtime'
        old = {'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40},
               'record_created_at': '2026-01-01T00:00:00Z', 'last_successful_check_at': None}
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(state), 'max_checks': 2,
                  'watches': [{'id': 'education', 'repository': 'https://github.com/owner/education',
                               'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local',
                               'legacy_observation': old},
                              {'id': 'other', 'repository': 'https://github.com/owner/other',
                               'kind': 'git_refs', 'consumer': 'other', 'delivery': 'local'}],
                  'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                      'relative_root': 'consumers/member/education',
                                      'accepted_receipt': 'synthetic-member-accepted-exact-path'}]}
        def observe(watch, previous):
            return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                    'head_sha': 'b' * 40, 'refs': {'HEAD': 'b' * 40}}, 'problems': []}
        first = tracker.run_tracking(config, now=1000, observe_watch=observe)
        target = state / 'consumers/member/education/latest.json'
        self.assertTrue(target.is_file(), 'accepted consumer has no durable local input')
        saved = json.loads(target.read_bytes())
        self.assertEqual(saved['watch_id'], 'education')
        self.assertEqual(saved['run_id'], first['run_id'])
        self.assertEqual(saved['legacy_observation'], old)
        self.assertIsNone(saved['legacy_observation']['last_successful_check_at'])
        self.assertEqual(saved['last_observation']['snapshot']['head_sha'], 'b' * 40)
        self.assertIn('since_legacy_baseline', saved, 'consumer input omits preserved old-to-current refs difference')
        self.assertEqual(saved['since_legacy_baseline']['status'], 'KNOWN_PUBLIC_REFS_ONLY')
        self.assertEqual(saved['since_legacy_baseline']['changed_refs'],
                         [{'ref': 'HEAD', 'before': 'a' * 40, 'after': 'b' * 40}])
        self.assertEqual(saved['since_legacy_baseline']['compare_url'],
                         'https://github.com/owner/education/compare/' + 'a' * 40 + '...' + 'b' * 40)
        self.assertEqual(len(saved['pending_events']), 1)
        pending_id = saved['pending_events'][0]['id']
        self.assertEqual(saved['pending_events'][0]['event']['watch_id'], 'education')
        self.assertNotIn('https://github.com/owner/other', target.read_text(encoding='utf-8'))
        tracker.run_tracking(config, now=1001, observe_watch=observe)
        later = json.loads(target.read_bytes())
        self.assertEqual(later['pending_events'][0]['id'], pending_id)
        self.assertEqual(len(later['pending_events']), 1)
        self.assertFalse(later['member_has_read_or_adopted'])
        self.assertEqual(len(tracker.pending_reviews(config)), 2, 'projection must not acknowledge reviews')


    def test_consumer_view_rejects_invalid_receipt_path_or_database_before_effects(self):
        tracker = self.tracker()
        state = self.case / 'runtime'
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(state),
                  'watches': [{'id': 'education', 'repository': 'https://github.com/owner/education',
                               'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local'}],
                  'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                      'relative_root': 'consumers/member/education', 'accepted_receipt': True}]}
        calls = []
        def observe(watch, previous):
            calls.append(watch['id'])
            return {'status': 'OK', 'snapshot': {}, 'problems': []}
        with self.assertRaisesRegex(ValueError, 'CONSUMER_VIEW'):
            tracker.run_tracking(config, now=1000, observe_watch=observe)
        self.assertFalse(state.exists())
        self.assertEqual(calls, [])
        config['consumer_views'][0]['accepted_receipt'] = 'synthetic-accepted'
        config['consumer_views'][0]['relative_root'] = 'consumers/../escape'
        with self.assertRaisesRegex(ValueError, 'CONSUMER_VIEW'):
            tracker.run_tracking(config, now=1000, observe_watch=observe)
        self.assertFalse(state.exists())
        config['consumer_views'][0]['relative_root'] = 'consumers/member/education'
        self.assertEqual(tracker.project_consumer_views(config), [])
        self.assertFalse(state.exists(), 'projection-only read must not create an empty state/database')


    def test_consumer_projection_failure_keeps_committed_run_and_repairs_without_new_observation(self):
        from unittest.mock import patch
        tracker = self.tracker()
        state = self.case / 'runtime'
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(state),
                  'watches': [{'id': 'education', 'repository': 'https://github.com/owner/education',
                               'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local'}],
                  'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                      'relative_root': 'consumers/member/education', 'accepted_receipt': 'synthetic-accepted'}]}
        calls = []
        def observe(watch, previous):
            calls.append(watch['id'])
            return {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
        with patch.object(Path, 'replace', side_effect=OSError('synthetic projection failure')):
            with self.assertRaisesRegex(OSError, 'projection failure'):
                tracker.run_tracking(config, now=1000, observe_watch=observe)
        self.assertEqual(calls, ['education'])
        self.assertEqual(len(tracker.pending_reviews(config)), 1)
        target = state / 'consumers/member/education/latest.json'
        self.assertFalse(target.exists())
        self.assertEqual(list(target.parent.glob('.latest-*')), [])
        self.assertEqual(tracker.project_consumer_views(config), [str(target)])
        saved = json.loads(target.read_bytes())
        self.assertEqual(saved['last_good_snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(len(saved['pending_events']), 1)
        tracker.run_tracking(config, now=1001, observe_watch=observe)
        self.assertEqual(calls, ['education'], 'repair must not repeat the committed observation')
        self.assertEqual(len(tracker.pending_reviews(config)), 1)


    def test_consumer_view_preserves_actual_source_check_time_when_budget_skips_it(self):
        import copy
        tracker = self.tracker()
        education = {'id': 'education', 'repository': 'https://github.com/owner/education',
                     'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local', 'interval_hours': 1,
                     'legacy_observation': {'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40}}}
        other = dict(education, id='aaa', repository='https://github.com/owner/other', consumer='other')
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'source-times'),
                  'max_checks': 2, 'watches': [education, other],
                  'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                      'relative_root': 'consumers/member/education', 'accepted_receipt': 'synthetic-A'}]}
        calls = []
        def observe(watch, previous):
            calls.append(watch['id'])
            return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                    'head_sha': 'b' * 40, 'refs': {'HEAD': 'b' * 40}}, 'problems': []}
        first = tracker.run_tracking(config, now=1000, observe_watch=observe)
        config['max_checks'] = 1
        report = tracker.run_tracking(config, now=5000, observe_watch=observe)
        target = Path(config['state_root']) / 'consumers/member/education/latest.json'
        saved = json.loads(target.read_bytes())
        self.assertEqual(calls, ['aaa', 'education', 'aaa'])
        self.assertEqual(report['watches']['education']['checked_at'], 1000)
        self.assertEqual(saved.get('checked_at'), 1000, 'actual source check time missing behind new global run')
        self.assertEqual(saved['last_good_checked_at'], 1000)
        self.assertFalse(saved['checked_in_run'])
        self.assertEqual(saved['freshness'], 'DUE_UNCHECKED')
        self.assertEqual(saved['observation_run_id'], first['run_id'])
        self.assertEqual(saved['last_good_run_id'], first['run_id'])
        self.assertEqual(saved['since_legacy_baseline']['observation_checked_at'], 1000)
        held = copy.deepcopy(config)
        held['watches'][0]['migration_hold'] = True
        tracker.run_tracking(held, now=5001, observe_watch=observe)
        self.assertEqual(json.loads(target.read_bytes())['freshness'], 'MIGRATION_HOLD')
        self.assertEqual(calls, ['aaa', 'education', 'aaa'])


    def test_consumer_pending_events_bind_source_and_exact_accepted_scope_without_deleting_history(self):
        import copy
        tracker = self.tracker()
        config = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'source-pending'),
                  'watches': [{'id': 'education', 'repository': 'https://github.com/owner/source-a',
                               'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local', 'interval_hours': 1}],
                  'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                      'relative_root': 'consumers/member/education', 'accepted_receipt': 'synthetic-accepted-A'}]}
        def observe(watch, previous):
            return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                    'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40}}, 'problems': []}
        tracker.run_tracking(config, now=1000, observe_watch=observe)
        old_pending = copy.deepcopy(tracker.pending_reviews(config))
        self.assertEqual(len(old_pending), 1)
        config['watches'][0]['repository'] = 'https://github.com/owner/source-b'
        config['consumer_views'][0]['accepted_receipt'] = 'synthetic-accepted-B'
        tracker.run_tracking(config, now=1001, observe_watch=observe)
        target = Path(config['state_root']) / 'consumers/member/education/latest.json'
        saved = json.loads(target.read_bytes())
        self.assertEqual([e['event']['repository'] for e in saved['pending_events']],
                         ['https://github.com/owner/source-b'], 'old source pending was delivered under new source acceptance')
        self.assertEqual(tracker.pending_reviews(config)[0], old_pending[0], 'old outbox evidence was changed or deleted')
        self.assertEqual(len(tracker.pending_reviews(config)), 2)
        # Even unchanged public source cannot carry an old exact consumer grant to a new recipient/path.
        config['consumer_views'][0]['accepted_receipt'] = 'synthetic-accepted-B-new-scope'
        config['consumer_views'][0]['relative_root'] = 'consumers/member/new-scope'
        tracker.run_tracking(config, now=1002, observe_watch=observe)
        other = Path(config['state_root']) / 'consumers/member/new-scope/latest.json'
        self.assertEqual(json.loads(other.read_bytes())['pending_events'], [])
        self.assertEqual(len(tracker.pending_reviews(config)), 2, 'scope change must not auto-acknowledge old events')


    def test_stale_publisher_cannot_use_new_source_or_new_exact_acceptance(self):
        import copy
        tracker = self.tracker()
        original = {'discovery_roots': [str(self.sources)], 'state_root': str(self.case / 'stale-publisher'),
                    'watches': [{'id': 'education', 'repository': 'https://github.com/owner/source-a',
                                 'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local', 'interval_hours': 1}],
                    'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                        'relative_root': 'consumers/member/source-a', 'accepted_receipt': 'synthetic-A'}]}
        def observe(watch, previous):
            return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                    'head_sha': 'b' * 40, 'refs': {'HEAD': 'b' * 40}}, 'problems': []}
        tracker.run_tracking(original, now=1000, observe_watch=observe)
        target = Path(original['state_root']) / 'consumers/member/source-a/latest.json'
        before = target.read_bytes()
        newer = copy.deepcopy(original)
        newer['watches'][0]['repository'] = 'https://github.com/owner/source-b'
        newer['consumer_views'][0].update(relative_root='consumers/member/source-b', accepted_receipt='synthetic-B')
        tracker.run_tracking(newer, now=1001, observe_watch=observe)
        with self.assertRaisesRegex(ValueError, 'CONSUMER_VIEW_STALE_BINDING'):
            tracker.project_consumer_views(original)
        self.assertEqual(target.read_bytes(), before)
        newer_target = Path(newer['state_root']) / 'consumers/member/source-b/latest.json'
        source_b_before = newer_target.read_bytes()
        reaccepted = copy.deepcopy(newer)
        reaccepted['consumer_views'][0]['accepted_receipt'] = 'synthetic-B-reaccepted'
        tracker.run_tracking(reaccepted, now=1002, observe_watch=observe)
        reaccepted_bytes = newer_target.read_bytes()
        with self.assertRaisesRegex(ValueError, 'CONSUMER_VIEW_STALE_BINDING'):
            tracker.project_consumer_views(newer)
        self.assertEqual(newer_target.read_bytes(), reaccepted_bytes)
        self.assertNotEqual(source_b_before, reaccepted_bytes)


    def test_removed_watch_and_view_only_revocation_reject_stale_publish_without_touching_history(self):
        import copy
        from unittest.mock import patch
        tracker = self.tracker()
        for remove_watch in (False, True):
            with self.subTest(remove_watch=remove_watch):
                config = {'discovery_roots': [str(self.sources)],
                          'state_root': str(self.case / ('removed-watch' if remove_watch else 'removed-view')),
                          'watches': [{'id': 'education', 'repository': 'https://github.com/owner/education',
                                       'kind': 'git_refs', 'consumer': 'member', 'delivery': 'local'}],
                          'consumer_views': [{'watch_id': 'education', 'consumer': 'member',
                                              'relative_root': 'consumers/member/education', 'accepted_receipt': 'synthetic-accepted'}]}
                def observe(watch, previous):
                    return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                            'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40}}, 'problems': []}
                tracker.run_tracking(config, now=1000, observe_watch=observe)
                target = Path(config['state_root']) / 'consumers/member/education/latest.json'
                before = target.read_bytes()
                pending = copy.deepcopy(tracker.pending_reviews(config))
                revoked = copy.deepcopy(config)
                revoked['consumer_views'] = []
                if remove_watch:
                    revoked['watches'] = []
                report = tracker.run_tracking(revoked, now=1001, observe_watch=observe)
                self.assertEqual(report['watches']['education']['discovery_status'],
                                 'NO_LONGER_REFERENCED' if remove_watch else 'REFERENCED')
                with patch.object(Path, 'mkdir') as mkdir, patch.object(Path, 'replace') as replace:
                    with self.assertRaisesRegex(ValueError, 'CONSUMER_VIEW_STALE_BINDING'):
                        tracker.project_consumer_views(config)
                    mkdir.assert_not_called()
                    replace.assert_not_called()
                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(tracker.pending_reviews(config), pending)
                self.assertEqual(tracker.project_consumer_views(revoked), [])
                tracker.run_tracking(config, now=1002, observe_watch=observe)
                self.assertEqual(json.loads(target.read_bytes())['last_observation']['status'], 'OK')


if __name__ == '__main__':
    unittest.main()
