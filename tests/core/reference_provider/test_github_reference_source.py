"""Only synthetic fixtures; run through run_tests.py to deny networking."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent
REPOSITORY = "https://github.com/NousResearch/hermes-agent"
API = "/repos/NousResearch/hermes-agent"
SHA_A = "a" * 40
SHA_B = "b" * 40


def provider(case):
    path = ROOT.parents[2] / "scripts" / "github_reference_source.py"
    case.assertTrue(path.is_file(), "Missing repository observation capability")
    spec = importlib.util.spec_from_file_location("github_reference_source", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed():
    return json.loads((ROOT / "evidence" / "fixtures" / "repository.json").read_text(encoding="utf-8"))


class FixtureFetch:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, path):
        self.calls.append(path)
        if path not in self.routes:
            raise AssertionError("Unplanned API path: " + path)
        value = self.routes[path]
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)


def repo_fetch(data=None):
    data = data or seed()
    return FixtureFetch({
        API: (data["metadata"], {}),
        API + "/commits/main": (data["head"], {}),
        API + "/releases/latest": (data["release"], {}),
    })


class RepositoryTests(unittest.TestCase):
    def test_release_absence_vs_unknown_errors(self):
        module = provider(self)
        fetch = repo_fetch()
        fetch.routes[API + "/releases/latest"] = ({"message": "Not Found"}, {":status": 404})
        try:
            absent = module.observe_repository(REPOSITORY, fetch=fetch)
        except (KeyError, TypeError) as error:
            self.fail("Missing HTTP/absence semantics: " + type(error).__name__)
        self.assertEqual(absent["status"], "OK")
        self.assertIsNone(absent["snapshot"]["release"])
        self.assertEqual(absent["snapshot"]["coverage"]["release"], "ABSENT")
        self.assertEqual(absent["problems"], [])
        for status in (403, 429):
            with self.subTest(status=status):
                fetch = repo_fetch()
                fetch.routes[API + "/releases/latest"] = ({"message": "secret diagnostic"}, {
                    ":status": status, "X-RateLimit-Remaining": "0", "X-RateLimit-Limit": "60",
                    "X-RateLimit-Reset": "1900000000", "Retry-After": "9"})
                result = module.observe_repository(REPOSITORY, fetch=fetch)
                self.assertEqual(result["status"], "PARTIAL")
                self.assertEqual(result["snapshot"]["coverage"]["release"], "UNKNOWN")
                problem = result["problems"][0]
                self.assertEqual(problem["code"], "HTTP_" + str(status))
                self.assertEqual(problem["scope"], "release")
                self.assertEqual(problem["rate_limit"], {
                    "remaining": 0, "limit": 60, "reset": 1900000000, "retry_after": 9})
                self.assertNotIn("secret diagnostic", json.dumps(result))
        for failure, code in [(TimeoutError("secret"), "TIMEOUT"),
                              (OSError("secret"), "NETWORK_ERROR")]:
            fetch = repo_fetch()
            fetch.routes[API] = failure
            result = module.observe_repository(REPOSITORY, fetch=fetch)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["problems"][0]["code"], code)
            self.assertEqual(fetch.calls, [API])

    def test_schema_validation_and_public_only(self):
        module = provider(self)
        for metadata in [{}, {**seed()["metadata"], "private": True},
                         {**seed()["metadata"], "id": True},
                         {**seed()["metadata"], "full_name": "evil/elsewhere"},
                         {**seed()["metadata"], "default_branch": "../x"}]:
            fetch = repo_fetch()
            fetch.routes[API] = (metadata, {})
            try:
                result = module.observe_repository(REPOSITORY, fetch=fetch)
            except (KeyError, TypeError):
                self.fail("Malformed metadata must be UNKNOWN, never crash")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIn(result["problems"][0]["code"], ("INVALID_SCHEMA", "NOT_PUBLIC"))
            self.assertEqual(fetch.calls, [API])
        fetch = repo_fetch()
        fetch.routes[API + "/commits/main"] = ({"sha": "../escape"}, {})
        result = module.observe_repository(REPOSITORY, fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIsNone(result["snapshot"]["head_sha"])
        self.assertEqual(result["snapshot"]["coverage"]["head"], "UNKNOWN")
        data = seed()
        data["metadata"]["default_branch"] = "topic/safe"
        data["metadata"]["license"] = None
        fetch = repo_fetch(data)
        fetch.routes[API + "/commits/topic%2Fsafe"] = fetch.routes.pop(API + "/commits/main")
        result = module.observe_repository(REPOSITORY, fetch=fetch)
        self.assertEqual(result["status"], "OK")
        self.assertIsNone(result["snapshot"]["license"])

    def test_head_comparison_is_bounded_and_honest(self):
        module = provider(self)
        previous = {"repository": REPOSITORY, "repository_id": 991, "head_sha": SHA_B}
        fetch = repo_fetch()
        comparison = {
            "total_commits": 1,
            "commits": [{"sha": SHA_A, "commit": {"message": "Fix one\nmore details"}}],
            "files": [{"filename": "src/a.py", "status": "modified"}]}
        path = API + "/compare/" + SHA_B + "..." + SHA_A + "?per_page=100&page=1"
        fetch.routes[path] = (comparison, {})
        result = module.observe_repository(REPOSITORY, previous, fetch=fetch)
        self.assertIn("compare", result["snapshot"], "Missing changed-HEAD comparison")
        self.assertEqual(result["status"], "OK")
        compare = result["snapshot"]["compare"]
        self.assertTrue(compare["complete"])
        self.assertEqual(compare["changed_paths"], ["src/a.py"])
        self.assertEqual(compare["commits"], [{"sha": SHA_A, "title": "Fix one"}])
        self.assertEqual(len(fetch.calls), 4)
        comparison["total_commits"] = 101
        comparison["commits"] *= 100
        comparison["files"] *= 300
        fetch.routes[path] = (comparison, {"Link": '<https://api.github.com' + path.replace('page=1', 'page=2') + '>; rel="next"'})
        result = module.observe_repository(REPOSITORY, previous, fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["snapshot"]["compare"]["complete"])
        self.assertLessEqual(len(result["snapshot"]["compare"]["commits"]), 20)
        self.assertLessEqual(len(result["snapshot"]["compare"]["changed_paths"]), 20)
        self.assertIn("COMPARE_TRUNCATED", [p["code"] for p in result["problems"]])
        for previous in ({**previous, "head_sha": SHA_A},
                         {**previous, "head_sha": "../escape"},
                         {**previous, "repository": "https://github.com/Other/Repo"}):
            fetch = repo_fetch()
            result = module.observe_repository(REPOSITORY, previous, fetch=fetch)
            self.assertLessEqual(len(fetch.calls), 3)

    def test_release_malformed_payload_is_partial_and_tail_hash_is_stable(self):
        module = provider(self)
        for release in ({}, [], {**seed()["release"], "body": 1},
                        {**seed()["release"], "tag_name": "x" * 10000}):
            fetch = repo_fetch()
            fetch.routes[API + "/releases/latest"] = (release, {})
            try:
                result = module.observe_repository(REPOSITORY, fetch=fetch)
            except (KeyError, TypeError, AttributeError):
                self.fail("Malformed release must be isolated UNKNOWN, not crash")
            self.assertEqual(result["status"], "PARTIAL")
            self.assertIsNone(result["snapshot"]["release"])
            self.assertEqual(result["snapshot"]["coverage"]["release"], "UNKNOWN")
        data = seed()
        data["release"]["body"] = "文" * 500 + "tail1"
        one = module.observe_repository(REPOSITORY, fetch=repo_fetch(data))
        self.assertEqual(len(one["snapshot"]["release"]["preview"]), 300)
        data["release"]["body"] += "tail2"
        two = module.observe_repository(REPOSITORY, fetch=repo_fetch(data))
        self.assertNotEqual(one["snapshot"]["release"]["body_sha256"], two["snapshot"]["release"]["body_sha256"])
        self.assertEqual(two, module.observe_repository(REPOSITORY, fetch=repo_fetch(data)))

    def test_serialized_snapshot_stays_below_legacy_limit(self):
        module = provider(self)
        data = seed()
        data["release"]["body"] = "文" * 800
        fetch = repo_fetch(data)
        path = API + "/compare/" + SHA_B + "..." + SHA_A + "?per_page=100&page=1"
        fetch.routes[path] = ({"total_commits": 20, "commits": [
            {"sha": SHA_A, "commit": {"message": "文" * 200}} for _ in range(20)],
            "files": [{"filename": "文" * 300} for _ in range(20)]}, {})
        result = module.observe_repository(REPOSITORY, {"repository": REPOSITORY, "head_sha": SHA_B}, fetch=fetch)
        self.assertLessEqual(len(json.dumps(result)), 6500, "Bounded arrays alone do not bound serialized output")
        self.assertEqual(result["status"], "PARTIAL")
        self.assertIn("OUTPUT_TRUNCATED", [p["code"] for p in result["problems"]])
        self.assertFalse(result["snapshot"]["compare"]["complete"])

    def test_invalid_http_envelope_and_protocol_errors_are_unknown(self):
        module = provider(self)
        from http.client import BadStatusLine
        for value in (({}, None), ({}, {":status": "bogus"}), ({}, {":status": True}),
                      BadStatusLine("private upstream")):
            fetch = repo_fetch()
            fetch.routes[API] = value
            try:
                result = module.observe_repository(REPOSITORY, fetch=fetch)
            except (ValueError, AttributeError, BadStatusLine):
                self.fail("HTTP envelope/protocol failures must be bounded UNKNOWN")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertIn(result["problems"][0]["code"], ("INVALID_RESPONSE", "PROTOCOL_ERROR"))
        for license_value in ({"name": ["nested"]}, {"name": "x" * 50000}, {"spdx_id": float("nan")}):
            data = seed()
            data["metadata"]["license"] = license_value
            try:
                result = module.observe_repository(REPOSITORY, fetch=repo_fetch(data))
            except ValueError:
                self.fail("Bad license metadata must not break JSON serialization")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertLessEqual(len(json.dumps(result)), 6500)

    def test_strict_repository_tokens_rejected_without_fetch(self):
        module = provider(self)
        invalid = [
            "http://github.com/a/b", "https://github.com:443/a/b",
            "https://user@github.com/a/b", "https://github.com/a/../b",
            "https://github.com/a/b\n", "https://github.com/a/b?x=1",
            "https://github.com/a/b#main", "https://github.com/a/%62",
            "https://evil.test/a/b", "https://github.com/a/b/",
            "https://github.com/a/b.git", "https://github.com/-a/b",
            "https://github.com/a/..", "https://github.com/a/b/c",
            "https://github.com/a\\\\b", "https://github.com/a/b\r\nX: bad",
        ]
        for repository in invalid:
            with self.subTest(repository=repr(repository)):
                fetch = FixtureFetch({})
                result = module.observe_repository(repository, fetch=fetch)
                self.assertEqual(result["status"], "UNKNOWN")
                self.assertEqual(result["problems"][0]["code"], "INVALID_REPOSITORY")
                self.assertEqual(fetch.calls, [])
                self.assertIsNone(result["snapshot"]["repository"])

    def test_repository_snapshot(self):
        module = provider(self)
        fetch = repo_fetch()
        result = module.observe_repository(REPOSITORY, fetch=fetch)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["problems"], [])
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["repository"], REPOSITORY)
        self.assertEqual(snapshot["repository_id"], 991)
        self.assertEqual(snapshot["default_branch"], "main")
        self.assertEqual(snapshot["head_sha"], SHA_A)
        self.assertFalse(snapshot["archived"])
        self.assertEqual(snapshot["license"]["spdx_id"], "MIT")
        self.assertEqual(snapshot["license_scope"], "metadata_only")
        release = snapshot["release"]
        self.assertEqual(release["tag"], "v1.2.3")
        self.assertEqual(release["id"], 901)
        self.assertEqual(release["published_at"], "2026-01-01T00:00:00Z")
        body = seed()["release"]["body"]
        self.assertEqual(release["body_sha256"], hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(release["preview"], body)
        self.assertLessEqual(len(fetch.calls), 5)
        json.dumps(result, allow_nan=False)
