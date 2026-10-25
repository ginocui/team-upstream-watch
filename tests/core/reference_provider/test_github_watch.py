"""Offline compatibility observations for existing Hermes pull watches."""
import copy
import hashlib
import json
import unittest
from test_github_reference_source import API, REPOSITORY, ROOT, SHA_A, SHA_B, FixtureFetch, provider, seed


def pull_seed(number=134254):
    obj = json.loads((ROOT / "evidence" / "fixtures" / "pull.json").read_text(encoding="utf-8"))
    obj["number"] = number
    return obj


def watch_fetch(number=134254):
    return FixtureFetch({API: (seed()["metadata"], {}),
                         API + "/pulls/" + str(number): (pull_seed(number), {})})


class WatchTests(unittest.TestCase):
    def test_activity_summaries_hash_entire_bodies_and_filter_head_runs(self):
        module = provider(self)
        fetch = watch_fetch()
        comment_path = API + "/issues/134254/comments?per_page=100&page=1"
        review_path = API + "/pulls/134254/reviews?per_page=100&page=1"
        run_path = API + "/actions/runs?head_sha=" + SHA_A + "&per_page=100&page=1"
        huge_body = "X" * 15000 + "TAIL-ONE"
        fetch.routes[comment_path] = ([{"id": 6067568551, "updated_at": "2026-01-03T00:00:00Z", "body": huge_body}], {})
        fetch.routes[review_path] = ([{"id": 42, "submitted_at": "2026-01-04T00:00:00Z", "state": "APPROVED", "body": "review", "commit_id": SHA_A}], {})
        run = {"id": 71, "head_sha": SHA_A, "status": "completed", "conclusion": "success", "updated_at": "2026-01-04T00:00:00Z", "run_attempt": 1}
        fetch.routes[run_path] = ({"total_count": 1, "workflow_runs": [run]}, {})
        watch = {"repository": REPOSITORY, "kind": "pull", "number": 134254,
                 "comments": True, "reviews": True, "workflow_runs": True}
        first = module.observe_watch(watch, fetch=fetch)
        self.assertIn("comments", first["snapshot"], "Missing legacy comments/reviews/actions observation")
        self.assertEqual(first["status"], "OK")
        for facet in ("comments", "reviews", "workflow_runs"):
            summary = first["snapshot"][facet]
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["observed_count"], 1)
            self.assertEqual(len(summary["items"]), 1)
            self.assertEqual(len(summary["sha256"]), 64)
        item = first["snapshot"]["comments"]["items"][0]
        self.assertEqual(item["id"], 6067568551)
        self.assertEqual(item["body_sha256"], hashlib.sha256(huge_body.encode()).hexdigest())
        self.assertNotIn("body", item)
        self.assertEqual(first["snapshot"]["reviews"]["items"][0]["updated_at"], "2026-01-04T00:00:00Z")
        self.assertEqual(first["snapshot"]["workflow_runs"]["head_sha"], SHA_A)
        self.assertLess(len(json.dumps(first)), 7000)
        self.assertEqual(len(fetch.calls), 5)
        fetch.routes[comment_path][0][0]["body"] = huge_body.replace("TAIL-ONE", "TAIL-TWO")
        second = module.observe_watch(watch, first, fetch=fetch)
        self.assertNotEqual(first["snapshot"]["comments"]["sha256"], second["snapshot"]["comments"]["sha256"])
        self.assertNotIn("X" * 100, json.dumps(second))

    def test_pagination_caps_keep_partial_hash_without_claiming_complete(self):
        module = provider(self)
        watch = {"repository": REPOSITORY, "kind": "pull", "number": 134254, "comments": True}
        fetch = watch_fetch()
        path = API + "/issues/134254/comments?per_page=100&page="
        for page in range(1, 4):
            values = [{"id": (page - 1) * 100 + index + 1, "updated_at": "2026-01-04T00:00:00Z", "body": "s" * 100}
                      for index in range(100)]
            fetch.routes[path + str(page)] = (values, {"Link": '<https://api.github.com' + path + str(page + 1) + '>; rel="next"'})
        result = module.observe_watch(watch, fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL", "Pagination must not silently claim a complete first page")
        summary = result["snapshot"]["comments"]
        self.assertEqual(summary["observed_count"], 300)
        self.assertFalse(summary["complete"])
        self.assertEqual(result["snapshot"]["coverage"]["comments"], "PARTIAL")
        self.assertLessEqual(len(summary["items"]), 5)
        self.assertTrue(summary["items_truncated"])
        self.assertLess(len(json.dumps(result)), 7000)
        self.assertEqual(len(fetch.calls), 5)
        self.assertIn("PAGINATION_LIMIT", [p["code"] for p in result["problems"]])
        first_hash = summary["sha256"]
        fetch.routes[path + "3"][0][-1]["body"] += "tail change"
        second = module.observe_watch(watch, fetch=fetch)
        self.assertNotEqual(first_hash, second["snapshot"]["comments"]["sha256"])
        fetch.routes[path + "2"] = ({}, {":status": 429, "x-ratelimit-remaining": "0"})
        result = module.observe_watch(watch, fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(result["snapshot"]["coverage"]["comments"], "PARTIAL")
        self.assertFalse(result["snapshot"]["comments"]["complete"])
        self.assertEqual(result["snapshot"]["comments"]["observed_count"], 100)
        self.assertEqual(result["snapshot"]["coverage"]["pull"], "OK")
        fetch.routes[path + "1"] = ([], {"Link": '<https://evil.test/steal>; rel="next"'})
        result = module.observe_watch(watch, fetch=fetch)
        self.assertFalse(result["snapshot"]["comments"]["complete"])
        self.assertIn("INVALID_PAGINATION", [p["code"] for p in result["problems"]])

    def test_watch_invalid_tokens_and_malformed_activity_are_unknown(self):
        module = provider(self)
        valid = {"repository": REPOSITORY, "kind": "pull", "number": 134254}
        for watch in (None, {}, {**valid, "kind": "issue"}, {**valid, "number": True},
                      {**valid, "number": "134254"}, {**valid, "number": -1},
                      {**valid, "comments": "true"},
                      {**valid, "repository": "https://github.com:443/a/b"}):
            fetch = FixtureFetch({})
            try:
                result = module.observe_watch(watch, fetch=fetch)
            except (KeyError, TypeError, AttributeError):
                self.fail("Invalid watch must return UNKNOWN before any fetch")
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(fetch.calls, [])
        fetch = watch_fetch()
        fetch.routes[API] = ({**seed()["metadata"], "private": True}, {})
        result = module.observe_watch(valid, fetch=fetch)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(fetch.calls, [API])
        for pull in ({}, {**pull_seed(), "number": 120329},
                     {**pull_seed(), "head": {"sha": "../bad"}}):
            fetch = watch_fetch()
            fetch.routes[API + "/pulls/134254"] = (pull, {})
            result = module.observe_watch(valid, fetch=fetch)
            self.assertEqual(result["status"], "UNKNOWN")
        path = API + "/issues/134254/comments?per_page=100&page=1"
        for values in ([{"id": True, "body": "bad"}], [{"id": 2, "body": {}}], [None]):
            fetch = watch_fetch()
            fetch.routes[path] = (values, {})
            result = module.observe_watch({**valid, "comments": True}, fetch=fetch)
            self.assertEqual(result["status"], "PARTIAL")
            self.assertFalse(result["snapshot"]["comments"]["complete"])
            self.assertEqual(result["snapshot"]["coverage"]["comments"], "UNKNOWN")
        fetch = watch_fetch()
        path = API + "/actions/runs?head_sha=" + SHA_A + "&per_page=100&page=1"
        fetch.routes[path] = ({"total_count": 1, "workflow_runs": [
            {"id": 4, "head_sha": SHA_B, "status": "completed", "conclusion": "success", "updated_at": "2026-01-01T00:00:00Z", "run_attempt": 1}]}, {})
        result = module.observe_watch({**valid, "workflow_runs": True}, fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["snapshot"]["workflow_runs"]["complete"])
        self.assertEqual(result["snapshot"]["workflow_runs"]["items"], [])

    def test_pagination_metadata_and_duplicate_ids_cannot_claim_complete(self):
        module = provider(self)
        watch = {"repository": REPOSITORY, "kind": "pull", "number": 134254, "workflow_runs": True}
        path = API + "/actions/runs?head_sha=" + SHA_A + "&per_page=100&page=1"
        run = {"id": 71, "head_sha": SHA_A, "status": "completed", "conclusion": "success", "updated_at": "2026-01-04T00:00:00Z", "run_attempt": 1}
        for payload in ({"workflow_runs": []}, {"total_count": "1", "workflow_runs": [run]},
                        {"total_count": 1, "workflow_runs": [run, run]}):
            fetch = watch_fetch()
            fetch.routes[path] = (payload, {})
            result = module.observe_watch(watch, fetch=fetch)
            self.assertEqual(result["status"], "PARTIAL", "Invalid pagination metadata is not a complete empty list")
            self.assertFalse(result["snapshot"]["workflow_runs"]["complete"])
        fetch = watch_fetch()
        comment_path = API + "/issues/134254/comments?per_page=100&page=1"
        fetch.routes[comment_path] = ([], {"Link": "garbage rel=next"})
        result = module.observe_watch({**watch, "workflow_runs": False, "comments": True}, fetch=fetch)
        self.assertEqual(result["status"], "PARTIAL")
        self.assertFalse(result["snapshot"]["comments"]["complete"])

    def test_missing_nullable_pull_fields_are_unknown_not_uncaught(self):
        module = provider(self)
        for key in ("merge_commit_sha", "head.repo", "base.repo"):
            pull = pull_seed()
            if "." in key:
                side, field = key.split(".")
                pull[side].pop(field)
            else:
                pull.pop(key)
            fetch = watch_fetch()
            fetch.routes[API + "/pulls/134254"] = (pull, {})
            try:
                result = module.observe_watch({"repository": REPOSITORY, "kind": "pull", "number": 134254}, fetch=fetch)
            except KeyError:
                self.fail("Missing nullable fields must be UNKNOWN, not crash")
            self.assertEqual(result["status"], "UNKNOWN")

    def test_pull_watch_existing_numbers(self):
        module = provider(self)
        self.assertTrue(callable(getattr(module, "observe_watch", None)), "Missing pull watch capability")
        for number in (134254, 120329):
            fetch = watch_fetch(number)
            result = module.observe_watch({"repository": REPOSITORY, "kind": "pull", "number": number}, fetch=fetch)
            self.assertEqual(result["status"], "OK")
            snapshot = result["snapshot"]
            self.assertEqual(snapshot["number"], number)
            self.assertEqual(snapshot["state"], "open")
            self.assertIs(snapshot["merged"], False)
            self.assertIsNone(snapshot["merge_commit_sha"])
            self.assertEqual(snapshot["head"]["sha"], SHA_A)
            self.assertEqual(snapshot["base"]["repository"], REPOSITORY)
            self.assertEqual(snapshot["repository_id"], 991)
            body = pull_seed()["body"]
            self.assertEqual(snapshot["body_sha256"], hashlib.sha256(body.encode()).hexdigest())
            self.assertEqual(snapshot["preview"], body)
            self.assertLessEqual(len(fetch.calls), 2)
