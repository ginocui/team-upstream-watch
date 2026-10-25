"""Public CLI tracer: one observation, two independent reviewers."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'team_watch.py'


class TeamWorkflow(unittest.TestCase):
    def setUp(self):
        import os
        self.temp_root = Path(os.environ.get('TEAM_WATCH_TEST_ROOT', ROOT / '.test-tmp'))
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def module(self):
        self.assertTrue(MODULE.is_file(), 'Standalone multi-member CLI is not implemented')
        spec = importlib.util.spec_from_file_location('team_watch', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_one_source_is_observed_once_and_each_member_ack_is_independent(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            root = Path(directory)
            config_file = root / 'team.json'
            config_file.write_text(json.dumps({
                'schema': 'team-upstream-watch/1', 'state_root': 'state',
                'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                             'kind': 'git_refs', 'interval_hours': 1}],
                'members': [{'id': 'alice', 'sources': ['library']},
                            {'id': 'bob', 'sources': ['library']}]
            }), encoding='utf-8')
            config = module.load_config(config_file)
            calls = []
            def observer(watch, previous):
                calls.append(watch['id'])
                return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                        'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40}}, 'problems': []}
            report = module.collect(config, now=1000, observe_watch=observer)
            self.assertEqual(calls, ['library'])
            alice = module.inbox(config, 'alice')
            bob = module.inbox(config, 'bob')
            self.assertEqual(len(alice['events']), 1)
            self.assertEqual(len(bob['events']), 1)
            event_id = alice['events'][0]['id']
            self.assertEqual(event_id, bob['events'][0]['id'])
            module.acknowledge(config, 'alice', event_id, 'Reviewed; not installed')
            self.assertEqual(module.inbox(config, 'alice')['events'], [])
            self.assertEqual(len(module.inbox(config, 'bob')['events']), 1)
            self.assertFalse(report['adoption_changed'])
            module.collect(config, now=1001, observe_watch=observer)
            self.assertEqual(calls, ['library'])


    def test_changed_source_definition_does_not_deliver_old_events_to_member(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            root = Path(directory)
            config_file = root / 'team.json'
            data = {'schema': 'team-upstream-watch/1', 'state_root': 'state',
                    'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                                 'kind': 'git_refs', 'interval_hours': 1}],
                    'members': [{'id': 'alice', 'sources': ['library']}]}
            config_file.write_text(json.dumps(data), encoding='utf-8')
            first = module.load_config(config_file)
            def observer(watch, previous):
                return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                        'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40}}, 'problems': []}
            module.collect(first, now=1000, observe_watch=observer)
            self.assertEqual(len(module.inbox(first, 'alice')['events']), 1)
            data['sources'][0]['repository'] = 'https://github.com/owner/other-library'
            config_file.write_text(json.dumps(data), encoding='utf-8')
            changed = module.load_config(config_file)
            self.assertEqual(module.inbox(changed, 'alice')['events'], [],
                             'Old source events must not follow a re-used source id')


    def test_cli_validates_relative_team_config_and_requires_network_opt_in(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            root = Path(directory)
            path = root / 'team.json'
            path.write_text(json.dumps({'schema': 'team-upstream-watch/1', 'state_root': 'state',
                'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                             'kind': 'git_refs'}],
                'members': [{'id': 'alice', 'sources': ['library']}]}), encoding='utf-8')
            good = subprocess.run([sys.executable, '-B', str(MODULE), 'validate', '--config', str(path)],
                                  capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr)
            self.assertTrue(good.stdout.strip(), 'CLI must return its validated result')
            result = json.loads(good.stdout)
            self.assertEqual(result['sources'], 1)
            self.assertEqual(result['members'], 1)
            denied = subprocess.run([sys.executable, '-B', str(MODULE), 'tick', '--config', str(path)],
                                    capture_output=True, text=True)
            self.assertEqual(denied.returncode, 2)
            self.assertIn('PUBLIC_NETWORK_NOT_ENABLED', denied.stderr)
            self.assertFalse((root / 'state').exists())


    def test_invalid_config_is_refused_before_creating_state(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'team.json'
            data = {'schema': 'team-upstream-watch/1', 'state_root': 'state',
                    'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                                 'kind': 'git_refs'}],
                    'members': [{'id': 'alice', 'sources': ['missing']}],
                    'managed_github': {'unexpected': 'not supported'}}
            path.write_text(json.dumps(data), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'CONFIG_INVALID'):
                module.load_config(path)
            self.assertFalse((path.parent / 'state').exists())


    def test_repeated_member_ack_is_idempotent_and_conflicts_are_refused(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'team.json'
            path.write_text(json.dumps({'schema': 'team-upstream-watch/1', 'state_root': 'state',
                'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                             'kind': 'git_refs'}],
                'members': [{'id': 'alice', 'sources': ['library']}]}), encoding='utf-8')
            config = module.load_config(path)
            def observer(watch, previous):
                return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                        'head_sha': 'a' * 40}, 'problems': []}
            module.collect(config, now=1000, observe_watch=observer)
            event = module.inbox(config, 'alice')['events'][0]['id']
            module.acknowledge(config, 'alice', event, 'Read only')
            module.acknowledge(config, 'alice', event, 'Read only')
            with self.assertRaisesRegex(ValueError, 'RECEIPT_CONFLICT'):
                module.acknowledge(config, 'alice', event, 'Different result')
            self.assertEqual(module.inbox(config, 'alice')['events'], [])


    def test_member_inbox_reports_cached_source_time_without_faking_new_check(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'team.json'
            path.write_text(json.dumps({'schema': 'team-upstream-watch/1', 'state_root': 'state',
                'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                             'kind': 'git_refs', 'interval_hours': 1}],
                'members': [{'id': 'alice', 'sources': ['library']}]}), encoding='utf-8')
            config = module.load_config(path)
            def observer(watch, previous):
                return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                        'head_sha': 'a' * 40}, 'problems': []}
            module.collect(config, now=1000, observe_watch=observer)
            module.collect(config, now=1001, observe_watch=observer)
            result = module.inbox(config, 'alice')
            self.assertIn('sources', result, 'Member inbox lacks per-source observation time')
            self.assertEqual(result['sources'][0]['checked_at'], 1000)
            self.assertEqual(result['sources'][0]['last_observation']['status'], 'OK')
            self.assertFalse(result['adoption_changed'])


    def test_unknown_recovery_is_deduplicated_and_retains_last_success(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'team.json'
            path.write_text(json.dumps({'schema': 'team-upstream-watch/1', 'state_root': 'state',
                'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                             'kind': 'git_refs', 'interval_hours': 1}],
                'members': [{'id': 'alice', 'sources': ['library']}]}), encoding='utf-8')
            config = module.load_config(path)
            success = {'status': 'OK', 'snapshot': {'repository': 'https://github.com/owner/library',
                       'head_sha': 'a' * 40}, 'problems': []}
            broken = {'status': 'UNKNOWN', 'snapshot': None, 'problems': [{'code': 'TIMEOUT'}]}
            module.collect(config, now=1000, observe_watch=lambda w, p: success)
            module.collect(config, now=5000, observe_watch=lambda w, p: broken)
            module.collect(config, now=9000, observe_watch=lambda w, p: broken)
            inbox = module.inbox(config, 'alice')
            self.assertEqual([item['event']['event'] for item in inbox['events']],
                             ['FIRST_OBSERVATION', 'OBSERVATION_UNKNOWN'])
            self.assertEqual(inbox['sources'][0]['last_good_checked_at'], 1000)
            module.collect(config, now=13000, observe_watch=lambda w, p: success)
            recovered = module.inbox(config, 'alice')
            self.assertEqual(recovered['events'][-1]['event']['event'], 'OBSERVATION_RECOVERED')
            self.assertFalse(recovered['adoption_changed'])


    def test_unknown_provider_or_bad_branch_config_cannot_validate_as_supported(self):
        module = self.module()
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            path = Path(directory) / 'team.json'
            base = {'schema': 'team-upstream-watch/1', 'state_root': 'state',
                    'sources': [{'id': 'library', 'repository': 'https://huggingface.co/owner/model',
                                 'kind': 'pull', 'number': 1}],
                    'members': [{'id': 'alice', 'sources': ['library']}]}
            for source in [base['sources'][0],
                           {'id': 'library', 'repository': 'https://github.com/owner/library',
                            'kind': 'atom', 'branch': '../escape'}]:
                base['sources'] = [source]
                path.write_text(json.dumps(base), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'CONFIG_INVALID'):
                    module.load_config(path)


class TeamRepair(unittest.TestCase):
    """Bounded regressions using real core modules and local CLI children."""
    def setUp(self):
        import os
        self.temp_root = Path(os.environ.get('TEAM_WATCH_TEST_ROOT', ROOT / '.test-tmp'))
        self.temp_root.mkdir(parents=True, exist_ok=True)

    def fixture(self, root):
        spec = importlib.util.spec_from_file_location('repair_team_watch', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = {'schema': 'team-upstream-watch/1', 'state_root': 'state',
                'sources': [{'id': 'library', 'repository': 'https://github.com/owner/library',
                             'kind': 'git_refs', 'interval_hours': 1}],
                'members': [{'id': 'alice', 'sources': ['library']},
                            {'id': 'bob', 'sources': ['library']}]}
        path = root / 'team.json'
        path.write_text(json.dumps(data), encoding='utf-8')
        return module, path, data, module.load_config(path)

    @staticmethod
    def observer(watch, previous):
        return {'status': 'OK', 'snapshot': {'repository': watch['repository'],
                'head_sha': 'a' * 40, 'refs': {'HEAD': 'a' * 40}}, 'problems': []}

    @staticmethod
    def cli(path, action, *args):
        import hashlib
        import subprocess
        import sys
        command = [sys.executable, '-B', str(MODULE), action, '--config', str(path), *args]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        print('TEAM_CLI ' + json.dumps({'command': command,
              'config': path.read_text(encoding='utf-8'),
              'config_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
              'native_exit': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}))
        return result

    def test_committed_first_event_survives_projection_failure(self):
        import sqlite3
        from contextlib import closing
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            module, path, data, config = self.fixture(Path(directory))
            state = Path(config['state_root'])
            state.mkdir()
            (state / 'runs').write_text('synthetic projection blocker', encoding='utf-8')
            with self.assertRaises(FileExistsError):
                module.collect(config, now=1000, observe_watch=self.observer)
            database = state / 'tracking.sqlite3'
            with closing(sqlite3.connect(database)) as conn:
                self.assertEqual(conn.execute('SELECT count(*) FROM runs').fetchone()[0], 1)
                event_id = conn.execute('SELECT id FROM review_outbox').fetchone()[0]
                self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='team_receipts'").fetchone())
            before = database.read_bytes()
            inbox = self.cli(path, 'inbox', '--member', 'alice')
            self.assertEqual(inbox.returncode, 0, inbox.stderr)
            self.assertEqual([row['id'] for row in json.loads(inbox.stdout)['events']], [event_id])
            self.assertEqual(database.read_bytes(), before, 'Inbox must not initialize receipt schema')
            ack = self.cli(path, 'ack', '--member', 'alice', '--event-id', event_id, '--receipt', 'Reviewed only')
            self.assertEqual(ack.returncode, 0, ack.stderr)
            self.assertEqual(module.inbox(config, 'alice')['events'], [])
            self.assertEqual(len(module.inbox(config, 'bob')['events']), 1)
            module.acknowledge(config, 'alice', event_id, 'Reviewed only')
            with self.assertRaisesRegex(ValueError, 'RECEIPT_CONFLICT'):
                module.acknowledge(config, 'alice', event_id, 'Conflicting review')


    def test_invalid_ack_without_collection_does_not_create_database(self):
        for existing_state in (False, True):
            with self.subTest(existing_state=existing_state):
                with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
                    module, path, data, config = self.fixture(Path(directory))
                    state = Path(config['state_root'])
                    if existing_state:
                        state.mkdir()
                    before = self.cli(path, 'inbox', '--member', 'alice')
                    self.assertEqual(before.returncode, 0, before.stderr)
                    denied = self.cli(path, 'ack', '--member', 'alice', '--event-id', 'not-an-event', '--receipt', 'Read only')
                    after = self.cli(path, 'inbox', '--member', 'alice')
                    self.assertEqual(denied.returncode, 2)
                    self.assertFalse((state / 'tracking.sqlite3').exists(), 'Denied ACK created a database')
                    self.assertEqual(state.exists(), existing_state)
                    self.assertEqual(after.returncode, 0, after.stderr)
                    self.assertEqual(json.loads(after.stdout)['events'], [])
                    self.assertEqual(denied.stderr.strip(), 'EVENT_NOT_PENDING_FOR_MEMBER')


    def test_member_database_paths_reject_hardlinks_like_collector(self):
        import os
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            root = Path(directory)
            module, path, data, config = self.fixture(root)
            module.collect(config, now=1000, observe_watch=self.observer)
            event_id = module.inbox(config, 'alice')['events'][0]['id']
            database = Path(config['state_root']) / 'tracking.sqlite3'
            alias = root / 'linked-state'
            alias.mkdir()
            os.link(database, alias / 'tracking.sqlite3')
            self.assertEqual(database.stat().st_nlink, 2)
            data['state_root'] = str(alias)
            path.write_text(json.dumps(data), encoding='utf-8')
            linked = module.load_config(path)
            before = database.read_bytes()
            try:
                with self.assertRaisesRegex(ValueError, 'STATE_REPARSE_OR_HARDLINK'):
                    module.collect(linked, now=1001, observe_watch=self.observer)
                for action, args in [('ack', ('--event-id', event_id, '--receipt', 'Read only')),
                                     ('inbox', ())]:
                    with self.subTest(action=action):
                        result = self.cli(path, action, '--member', 'alice', *args)
                        self.assertEqual(result.returncode, 2, result.stdout)
                        self.assertEqual(result.stderr.strip(), 'STATE_REPARSE_OR_HARDLINK')
                self.assertEqual(database.read_bytes(), before, 'Linked database must stay unchanged')
            finally:
                (alias / 'tracking.sqlite3').unlink()


    def test_migration_hold_key_is_rejected_even_when_false(self):
        for value in (True, False):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
                    module, path, data, config = self.fixture(Path(directory))
                    data['sources'][0]['migration_hold'] = value
                    path.write_text(json.dumps(data), encoding='utf-8')
                    result = self.cli(path, 'validate')
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn('CONFIG_UNSUPPORTED_MIGRATION_HOLD', result.stderr)
                    self.assertFalse(Path(config['state_root']).exists())


    def test_duplicate_json_keys_are_rejected_at_each_config_level(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            module, path, data, config = self.fixture(Path(directory))
            encoded = json.dumps(data)
            variants = [encoded[:-1] + ', "state_root": "other-state"}',
                        encoded.replace('"kind": "git_refs"', '"kind": "release", "kind": "git_refs"'),
                        encoded.replace('"id": "alice"', '"id": "other", "id": "alice"')]
            for raw in variants:
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding='utf-8')
                    result = self.cli(path, 'validate')
                    self.assertEqual(result.returncode, 2, result.stdout)
                    self.assertIn('CONFIG_DUPLICATE_KEY', result.stderr)
            self.assertFalse(Path(config['state_root']).exists())
            self.assertFalse((path.parent / 'other-state').exists())


    def test_state_root_with_nul_is_rejected_before_state_access(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            module, path, data, config = self.fixture(Path(directory))
            data['state_root'] = 'state\x00suffix'
            path.write_text(json.dumps(data), encoding='utf-8')
            result = self.cli(path, 'validate')
            self.assertEqual(result.returncode, 2, result.stdout)
            self.assertIn('CONFIG_INVALID', result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertFalse(Path(config['state_root']).exists())


    def test_repository_validation_matches_selected_provider_without_narrowing_refs(self):
        with tempfile.TemporaryDirectory(dir=self.temp_root) as directory:
            module, path, data, config = self.fixture(Path(directory))
            spec = importlib.util.spec_from_file_location('real_github_provider', MODULE.parent / 'scripts/github_reference_source.py')
            api = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(api)
            spec = importlib.util.spec_from_file_location('real_ref_provider', MODULE.parent / 'scripts/reference_watch_sources.py')
            refs = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(refs)
            invalid = ['https://github.com/owner_with_underscore/library',
                       'https://github.com/' + 'a' * 40 + '/library',
                       'https://github.com/owner/' + 'a' * 101,
                       'https://github.com/owner/library.GIT']
            for kind in ('repository', 'release', 'pull'):
                for repository in invalid:
                    with self.subTest(kind=kind, repository=repository):
                        self.assertIsNone(api._canonical(repository))
                        source = {'id': 'library', 'repository': repository, 'kind': kind}
                        if kind == 'pull':
                            source['number'] = 1
                        data['sources'] = [source]
                        path.write_text(json.dumps(data), encoding='utf-8')
                        result = self.cli(path, 'validate')
                        self.assertEqual(result.returncode, 2, result.stdout)
                        self.assertIn('CONFIG_INVALID_REPOSITORY', result.stderr)
            positive = [('repository', 'https://github.com/owner/library'),
                        ('release', 'https://github.com/owner/library'),
                        ('pull', 'https://github.com/owner/library'),
                        ('repository', 'https://huggingface.co/owner_with_underscore/model'),
                        ('git_refs', invalid[0]), ('atom', invalid[0]),
                        ('git_refs', invalid[1]), ('atom', invalid[2])]
            for kind, repository in positive:
                with self.subTest(kind=kind, repository=repository):
                    source = {'id': 'library', 'repository': repository, 'kind': kind}
                    if kind == 'pull':
                        source['number'] = 1
                    data['sources'] = [source]
                    path.write_text(json.dumps(data), encoding='utf-8')
                    result = self.cli(path, 'validate')
                    self.assertEqual(result.returncode, 0, result.stderr)
                    if kind == 'git_refs':
                        observed = refs.observe_git_refs(source, command=lambda args: (0, b'a' * 40 + b'\tHEAD\n', b''))
                        self.assertEqual(observed['status'], 'OK')
                    elif kind == 'atom':
                        head = 'a' * 40
                        feed = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>head</id>'
                                '<link href="' + repository + '/commit/' + head + '"/></entry></feed>').encode()
                        observed = refs.observe_atom(source, fetch=lambda url: feed if '/commits/' in url else b'<feed xmlns="http://www.w3.org/2005/Atom"/>')
                        self.assertEqual(observed['status'], 'OK')
                    elif repository.startswith('https://huggingface.co/'):
                        observed = refs.observe_huggingface(repository, fetch=lambda url: {'id': 'owner_with_underscore/model', 'sha': 'a' * 40})
                        self.assertEqual(observed['status'], 'OK')
                    else:
                        self.assertEqual(api._canonical(repository), repository)
            self.assertFalse(Path(config['state_root']).exists())


if __name__ == '__main__':
    unittest.main()
