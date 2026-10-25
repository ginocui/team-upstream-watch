"""Public extraction contracts exercised against the real core modules."""
import importlib
import os
from pathlib import Path
import tempfile
import unittest


class PublicBoundaryTests(unittest.TestCase):
    def test_managed_github_is_explicitly_unsupported_before_state_or_observation(self):
        tracker = importlib.import_module('reference_tracker')
        with tempfile.TemporaryDirectory(dir=os.environ['TEAM_WATCH_TEST_ROOT']) as directory:
            case = Path(directory)
            for index, managed in enumerate(({}, False, {'adapter': 'synthetic'})):
                with self.subTest(managed=managed):
                    state = case / ('state-' + str(index))
                    config = {'state_root': str(state), 'discovery_roots': [],
                              'managed_github': managed,
                              'watches': [{'id': 'pull', 'kind': 'pull', 'number': 7,
                                           'repository': 'https://github.com/example/project'}]}
                    try:
                        tracker.run_tracking(config, now=1000)
                    except Exception as error:
                        self.assertIsInstance(error, ValueError)
                        self.assertEqual(str(error), 'MANAGED_GITHUB_UNSUPPORTED')
                    else:
                        self.fail('Non-None managed_github was silently accepted')
                    self.assertFalse(state.exists(), 'Unsupported mode must fail before local writes')

    def test_derived_metadata_is_unsupported_for_every_public_observer(self):
        source = importlib.import_module('github_reference_source')
        from test_github_reference_source import API, REPOSITORY, FixtureFetch, seed
        observers = (
            ('repository', lambda evidence, fetch: source.observe_repository(
                REPOSITORY, fetch=fetch, metadata_evidence=evidence)),
            ('release', lambda evidence, fetch: source.observe_release(
                {'kind': 'release', 'repository': REPOSITORY}, fetch=fetch, metadata_evidence=evidence)),
            ('pull', lambda evidence, fetch: source.observe_watch(
                {'kind': 'pull', 'repository': REPOSITORY, 'number': 134254},
                fetch=fetch, metadata_evidence=evidence)),
        )
        previously_accepted = {'data': seed()['metadata'],
                               'source_endpoint': API + '/pulls/134254',
                               'source_json_path': 'base.repo', 'source_http_status': 200,
                               'source_operation': 'synthetic-read',
                               'source_response_sha256': 'c' * 64}
        for name, observe in observers:
            for evidence in (previously_accepted, {}, False, 'untrusted'):
                with self.subTest(observer=name, evidence=evidence):
                    # A valid direct GET route makes an old accepted-evidence path
                    # fail with an assertion, not an unrelated unplanned-fetch error.
                    fetch = FixtureFetch({API: (seed()['metadata'], {}),
                                          API + '/commits/main': (seed()['head'], {}),
                                          API + '/releases/latest': (seed()['release'], {}),
                                          API + '/pulls/134254': ({}, {})})
                    result = observe(evidence, fetch)
                    self.assertEqual(result['status'], 'UNKNOWN')
                    self.assertEqual(result['snapshot']['coverage']['metadata'], 'UNKNOWN')
                    self.assertEqual(result['problems'], [
                        {'code': 'METADATA_EVIDENCE_UNSUPPORTED', 'scope': 'metadata'}])
                    self.assertNotIn('metadata_source', result['snapshot'])
                    self.assertEqual(fetch.calls, [], 'Unsupported evidence must not fall back to HTTP')

    def test_managed_none_keeps_synthetic_injected_observation_unchanged(self):
        tracker = importlib.import_module('reference_tracker')
        with tempfile.TemporaryDirectory(dir=os.environ['TEAM_WATCH_TEST_ROOT']) as directory:
            state = Path(directory) / 'state'
            calls = []
            def observe(watch, previous):
                calls.append((watch['number'], previous))
                return {'status': 'OK', 'snapshot': {'head_sha': 'a' * 40}, 'problems': []}
            config = {'state_root': str(state), 'managed_github': None,
                      'watches': [{'id': 'pull', 'kind': 'pull', 'number': 7,
                                   'repository': 'https://github.com/example/project'}]}
            report = tracker.run_tracking(config, now=1000, observe_watch=observe)
            self.assertEqual(calls, [(7, None)])
            self.assertEqual(report['events'][0]['event'], 'FIRST_OBSERVATION')
            self.assertEqual(report['watches']['pull']['last_good_snapshot'], {'head_sha': 'a' * 40})
            self.assertFalse(report['adoption_changed'])

    def test_managed_mode_cannot_bypass_rejection_with_injected_observer_or_empty_watches(self):
        tracker = importlib.import_module('reference_tracker')
        with tempfile.TemporaryDirectory(dir=os.environ['TEAM_WATCH_TEST_ROOT']) as directory:
            for index, watches in enumerate(([], [{'id': 'pull', 'kind': 'pull', 'number': 7,
                                                   'repository': 'https://github.com/example/project'}])):
                state = Path(directory) / str(index)
                with self.subTest(watches=watches):
                    with self.assertRaisesRegex(ValueError, '^MANAGED_GITHUB_UNSUPPORTED$'):
                        tracker.run_tracking({'state_root': str(state), 'watches': watches, 'managed_github': {}},
                                             now=1000, observe_watch=lambda a, b: self.fail('Must not observe'))
                    self.assertFalse(state.exists())

    def test_public_direct_get_supports_unrelated_pull_and_release(self):
        source = importlib.import_module('github_reference_source')
        from test_github_reference_source import API, REPOSITORY, repo_fetch
        from test_github_watch import watch_fetch
        fetch = watch_fetch(number=7)
        pull = source.observe_watch({'kind': 'pull', 'repository': REPOSITORY, 'number': 7},
                                    metadata_evidence=None, fetch=fetch)
        self.assertEqual(pull['status'], 'OK')
        self.assertEqual(pull['snapshot']['number'], 7)
        self.assertEqual(fetch.calls, [API, API + '/pulls/7'])
        fetch = repo_fetch()
        release = source.observe_release({'kind': 'release', 'repository': REPOSITORY},
                                         metadata_evidence=None, fetch=fetch)
        self.assertEqual(release['status'], 'OK')
        self.assertEqual(release['snapshot']['coverage']['head'], 'NOT_REQUESTED')
        self.assertEqual(release['snapshot']['release']['tag'], 'v1.2.3')
        self.assertEqual(fetch.calls, [API, API + '/releases/latest'])

    def test_managed_cli_reports_explicit_unsupported_without_state(self):
        import json
        from offline import tracker_cli
        with tempfile.TemporaryDirectory(dir=os.environ['TEAM_WATCH_TEST_ROOT']) as directory:
            case = Path(directory)
            state = case / 'state'
            path = case / 'config.json'
            path.write_text(json.dumps({'state_root': str(state), 'managed_github': {}, 'watches': []}),
                            encoding='utf-8')
            result = tracker_cli(['tick', '--config', str(path), '--allow-public-network'])
            self.assertEqual(result.returncode, 2)
            self.assertEqual(result.stderr.strip(), b'MANAGED_GITHUB_UNSUPPORTED')
            self.assertEqual(result.stdout, b'')
            self.assertFalse(state.exists())


if __name__ == '__main__':
    unittest.main()
