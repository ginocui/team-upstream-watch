"""Offline transport tests replace HTTPSConnection, never a real server."""
import json
import ssl
import time
import unittest
from unittest.mock import patch
from test_github_reference_source import API, REPOSITORY, provider, repo_fetch, seed


class Response:
    def __init__(self, obj, status=200, headers=None, raw=None):
        self.status = status
        self.payload = raw if raw is not None else json.dumps(obj).encode()
        self.headers = headers or {}
        self.offset = 0

    def getheaders(self):
        return list(self.headers.items())

    def read(self, n):
        value = self.payload[self.offset:self.offset + n]
        self.offset += len(value)
        return value


class Connection:
    instances = []
    responses = []

    def __init__(self, host, *, timeout, context):
        self.host, self.timeout, self.context = host, timeout, context
        self.closed = False
        self.requests = []
        self.__class__.instances.append(self)

    def request(self, method, path, *, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        response = self.__class__.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def test_transport_bounds_deadlines_bytes_json_and_api_path(self):
        module = provider(self)
        self.assertTrue(callable(getattr(module, "_HTTPFetch", None)))
        self.assertTrue(isinstance(getattr(module, "MAX_RESPONSE_BYTES", None), int), "Missing byte/deadline bounds")
        cases = [(Response({}, raw=b"x" * (module.MAX_RESPONSE_BYTES + 1)), "BYTE_LIMIT"),
                 (Response({}, raw=b"not-json"), "INVALID_JSON"),
                 (Response({}, headers={"Content-Encoding": "gzip"}), "UNSUPPORTED_ENCODING"),
                 (TimeoutError("private error"), "TIMEOUT"),
                 (OSError("private error"), "NETWORK_ERROR")]
        for response, code in cases:
            Connection.instances = []
            Connection.responses = [response]
            with patch.object(module.http.client, "HTTPSConnection", Connection):
                result = module.observe_repository(REPOSITORY)
            self.assertEqual(result["status"], "UNKNOWN")
            self.assertEqual(result["problems"][0]["code"], code)
            self.assertTrue(Connection.instances[0].closed)
        with patch.object(module.http.client, "HTTPSConnection", Connection):
            fetch = module._HTTPFetch()
            fetch.deadline = module.time.monotonic() - 1
            problems = []
            obj, headers, state = module._request(fetch, API, "metadata", problems)
            self.assertEqual(state, "UNKNOWN")
            self.assertEqual(problems[0]["code"], "DEADLINE_EXCEEDED")
        for path in ("https://evil.test/x", "//evil.test/x", API + "/../x", API + "\r\nX:bad"):
            fetch = module._HTTPFetch()
            with self.assertRaises(module._FetchError) as ctx:
                fetch(path)
            self.assertEqual(ctx.exception.code, "INVALID_API_PATH")
        class SlowConnection(Connection):
            def getresponse(self):
                time.sleep(0.12)
                return Response(seed()["metadata"])
        with patch.object(module.http.client, "HTTPSConnection", SlowConnection):
            fetch = module._HTTPFetch()
            fetch.deadline = module.time.monotonic() + 0.03
            problems = []
            start = time.monotonic()
            _, _, state = module._request(fetch, API, "metadata", problems)
            self.assertEqual(state, "UNKNOWN")
            self.assertLess(time.monotonic() - start, 0.11)
        time.sleep(0.13)

    def test_total_byte_budget_and_request_timeout_are_terminal(self):
        module = provider(self)
        fetch = module._HTTPFetch()
        fetch.bytes_read = module.MAX_TOTAL_BYTES
        Connection.responses = [Response(seed()["metadata"])]
        with patch.object(module.http.client, "HTTPSConnection", Connection):
            problems = []
            _, _, state = module._request(fetch, API, "metadata", problems)
        self.assertEqual(state, "UNKNOWN")
        self.assertEqual(problems[0]["code"], "BYTE_LIMIT")
        with patch.object(module.http.client, "HTTPSConnection", Connection):
            fetch = module._HTTPFetch()
            for response, code in ((Response({}, raw=b"[" * 5000), "INVALID_JSON"),
                                   (Response({}, headers={"content-length": str(module.MAX_RESPONSE_BYTES + 1)}), "BYTE_LIMIT")):
                Connection.responses = [response]
                problems = []
                module._request(fetch, API, "metadata", problems)
                self.assertEqual(problems[0]["code"], code)

    def test_default_compare_path_accepts_sha_separator(self):
        module = provider(self)
        data = seed()
        old = "b" * 40
        obj = {"total_commits": 0, "commits": [], "files": []}
        Connection.instances = []
        Connection.responses = [Response(data["metadata"]), Response(data["head"]),
                                Response(data["release"]), Response(obj)]
        with patch.object(module.http.client, "HTTPSConnection", Connection):
            result = module.observe_repository(REPOSITORY, {"repository": REPOSITORY, "head_sha": old})
        self.assertEqual(result["status"], "OK")
        self.assertTrue(result["snapshot"]["compare"]["complete"])
        self.assertEqual(len(Connection.instances), 4)

    def test_default_transport_uses_only_fixed_https_get_and_no_redirect(self):
        module = provider(self)
        self.assertTrue(callable(getattr(module, "_HTTPFetch", None)), "Missing bounded default HTTPS transport")
        data = seed()
        Connection.instances = []
        Connection.responses = [Response(data["metadata"]), Response(data["head"]), Response(data["release"])]
        with patch.object(module.http.client, "HTTPSConnection", Connection):
            result = module.observe_repository(REPOSITORY)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(len(Connection.instances), 3)
        for conn in Connection.instances:
            self.assertEqual(conn.host, "api.github.com")
            self.assertLessEqual(conn.timeout, 8)
            self.assertGreater(conn.timeout, 0)
            self.assertTrue(conn.context.check_hostname)
            self.assertEqual(conn.context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(conn.closed)
            method, path, headers = conn.requests[0]
            self.assertEqual(method, "GET")
            self.assertTrue(path.startswith(API))
            self.assertFalse({"authorization", "cookie", "proxy-authorization"} & {k.lower() for k in headers})
        Connection.instances = []
        Connection.responses = [Response({}, 302, {"location": "https://evil.test/steal"})]
        with patch.object(module.http.client, "HTTPSConnection", Connection):
            result = module.observe_repository(REPOSITORY)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(len(Connection.instances), 1)
        self.assertEqual(result["problems"][0]["code"], "HTTP_302")
