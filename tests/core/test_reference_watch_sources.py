"""Synthetic coverage contracts for migrated non-API public-source duties."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'scripts/reference_watch_sources.py'


class Sources(unittest.TestCase):
    def source(self):
        self.assertTrue(MODULE.is_file(), 'migrated public git refs/Atom watch sources missing')
        spec = importlib.util.spec_from_file_location('watch_sources', MODULE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_git_refs_preserves_head_all_branches_tags_and_no_credential_route(self):
        source = self.source()
        raw = ('a' * 40 + '\tHEAD\n' + 'b' * 40 + '\trefs/heads/main\n' + 'c' * 40 + '\trefs/tags/v1\n').encode()
        calls = []
        def command(argv):
            calls.append(argv)
            return 0, raw, b''
        report = source.observe_git_refs({'repository': 'https://github.com/owner/repo'}, command=command)
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(len(report['snapshot']['refs']), 3)
        self.assertEqual(report['snapshot']['head_sha'], 'a' * 40)
        self.assertIn('credential.helper=', calls[0])
        self.assertIn('core.askPass=', calls[0])
        self.assertFalse(report['snapshot']['adoption_changed'])
        unknown = source.observe_git_refs({'repository': 'https://github.com/owner/repo'}, command=lambda a: (1, b'', b'synthetic'))
        self.assertEqual(unknown['status'], 'UNKNOWN')
        with self.assertRaises(ValueError):
            source.observe_git_refs({'repository': 'https://github.com/owner/repo;bad'}, command=command)


    def test_atom_commit_and_latest_five_release_bodies_detect_tail_edits_without_api(self):
        source = self.source()
        self.assertTrue(callable(getattr(source, 'observe_atom', None)), 'missing existing Atom source contract')
        prefix = '<feed xmlns="http://www.w3.org/2005/Atom">'
        entries = ''.join('<entry><id>tag:github.com,2008:Repository/x/v%d</id><title>v%d</title><updated>2026-01-01T00:00:00Z</updated><link href="https://github.com/owner/repo/releases/tag/v%d"/><content>%s</content></entry>' % (i, i, i, 'a' * 700 + str(i)) for i in range(6))
        release = (prefix + entries + '</feed>').encode()
        commits = (prefix + '<entry><id>tag:github.com,2008:Grit::Commit/' + 'a' * 40 + '</id><title>Changed</title><updated>2026-01-01T00:00:00Z</updated><link href="https://github.com/owner/repo/commit/' + 'a' * 40 + '"/></entry></feed>').encode()
        calls = []
        def fetch(path):
            calls.append(path)
            return commits if path.endswith('main.atom') else release
        report = source.observe_atom({'repository': 'https://github.com/owner/repo', 'branch': 'main'}, fetch=fetch)
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(report['snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(len(report['snapshot']['releases']), 5)
        self.assertTrue(all(len(r['preview']) <= 300 for r in report['snapshot']['releases']))
        self.assertTrue(all('api.github.com' not in p for p in calls))
        changed = source.observe_atom({'repository': 'https://github.com/owner/repo', 'branch': 'main'},
                    fetch=lambda p: commits if p.endswith('main.atom') else release.replace(b'aaaaaa0', b'aaaaab0'))
        self.assertNotEqual(report['snapshot']['releases'][0]['body_sha256'], changed['snapshot']['releases'][0]['body_sha256'])
        failed = source.observe_atom({'repository': 'https://github.com/owner/repo', 'branch': 'main'}, fetch=lambda p: b'bad xml')
        self.assertEqual(failed['status'], 'UNKNOWN')


    def test_huggingface_metadata_preserves_model_identity_without_weight_downloads(self):
        source = self.source()
        self.assertTrue(callable(getattr(source, 'observe_huggingface', None)), 'missing explicitly referenced model-source observation')
        calls = []
        def fetch(path):
            calls.append(path)
            return {'id': 'owner/model', 'sha': 'a' * 40, 'lastModified': '2026-01-01T00:00:00Z',
                    'private': False, 'gated': False, 'cardData': {'license': 'non-commercial'}}
        report = source.observe_huggingface('https://huggingface.co/owner/model', fetch=fetch)
        self.assertEqual(report['status'], 'OK')
        self.assertEqual(report['snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(report['snapshot']['license'], 'non-commercial')
        self.assertEqual(calls, ['/api/models/owner/model'])
        changed = source.observe_huggingface('https://huggingface.co/owner/model', fetch=lambda p: {'id': 'other/model', 'sha': 'b' * 40})
        self.assertEqual(changed['status'], 'UNKNOWN')
        with self.assertRaises(ValueError):
            source.observe_huggingface('https://huggingface.co/owner/../secret', fetch=fetch)


    def test_public_fallback_observes_head_and_release_feed_without_guessing_branch(self):
        source = self.source()
        self.assertTrue(callable(getattr(source, 'observe_public_fallback', None)), 'missing bounded public fallback')
        repo = 'https://github.com/owner/repo'
        prefix = '<feed xmlns="http://www.w3.org/2005/Atom">'
        feed = (prefix + '<entry><id>v1</id><title>Release</title><updated>2026-01-01T00:00:00Z</updated>'
                '<link href="https://github.com/owner/repo/releases/tag/v1"/><content>notes</content></entry></feed>').encode()
        calls = []
        def fetch(path):
            calls.append(path)
            return feed
        def command(argv):
            return 0, ('a' * 40 + '\tHEAD\n').encode(), b''
        observed = source.observe_public_fallback(repo, command=command, fetch=fetch)
        self.assertEqual(observed['status'], 'PARTIAL')
        self.assertEqual(observed['snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(observed['snapshot']['releases'][0]['id'], 'v1')
        self.assertEqual(calls, ['/owner/repo/releases.atom'])
        self.assertEqual(observed['snapshot']['coverage']['api_metadata'], 'UNKNOWN')
        failed = source.observe_public_fallback(repo, command=command, fetch=lambda p: b'bad')
        self.assertEqual(failed['status'], 'PARTIAL')
        self.assertEqual(failed['snapshot']['head_sha'], 'a' * 40)
        self.assertEqual(failed['snapshot']['coverage']['release_feed'], 'UNKNOWN')
        empty = source.observe_public_fallback(repo, command=command, fetch=lambda p: (prefix + '</feed>').encode())
        self.assertEqual(empty['snapshot']['releases'], [])
        self.assertEqual(empty['snapshot']['coverage']['release_feed'], 'OK')


if __name__ == '__main__':
    unittest.main()
