"""Bounded, read-only public GitHub observations for reference tracking."""
import hashlib
import http.client
import ssl
import time
import threading
import json
import re
from urllib.parse import quote


MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
TOTAL_SECONDS = 25


class _FetchError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class _HTTPFetch:
    def __init__(self):
        self.deadline = time.monotonic() + TOTAL_SECONDS
        self.context = ssl.create_default_context()
        self.bytes_read = 0
        self.poisoned = False

    def _read(self, api_path, timeout):
        connection = http.client.HTTPSConnection("api.github.com", timeout=timeout, context=self.context)
        try:
            connection.request("GET", api_path, headers={"Accept": "application/vnd.github+json",
                               "User-Agent": "WE-public-reference-observer/1",
                               "X-GitHub-Api-Version": "2022-11-28", "Accept-Encoding": "identity"})
            response = connection.getresponse()
            headers = {key.lower(): value for key, value in response.getheaders()}
            headers[":status"] = response.status
            if response.status != 200:
                return None, headers
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise _FetchError("UNSUPPORTED_ENCODING")
            limit = min(MAX_RESPONSE_BYTES, MAX_TOTAL_BYTES - self.bytes_read)
            length = headers.get("content-length")
            if length is not None and (not str(length).isdigit() or int(length) > limit):
                raise _FetchError("BYTE_LIMIT")
            payload = bytearray()
            while True:
                if time.monotonic() >= self.deadline:
                    raise _FetchError("DEADLINE_EXCEEDED")
                data = response.read(min(65536, limit + 1 - len(payload)))
                if not data:
                    break
                payload.extend(data)
                self.bytes_read += len(data)
                if len(payload) > limit:
                    raise _FetchError("BYTE_LIMIT")
            try:
                return json.loads(payload.decode("utf-8")), headers
            except (ValueError, UnicodeError, RecursionError):
                raise _FetchError("INVALID_JSON") from None
        finally:
            connection.close()

    def __call__(self, api_path):
        if (not isinstance(api_path, str) or not re.fullmatch(r"/repos/[A-Za-z0-9_.%/?=&-]+", api_path)
                or any(part in (".", "..") for part in api_path.split("?", 1)[0].split("/")) or "//" in api_path):
            raise _FetchError("INVALID_API_PATH")
        remaining = self.deadline - time.monotonic()
        if self.poisoned or remaining <= 0:
            raise _FetchError("DEADLINE_EXCEEDED")
        timeout = min(8, remaining)
        box = []
        ready = threading.Event()
        def work():
            try:
                box.append((True, self._read(api_path, timeout)))
            except Exception as error:
                box.append((False, error))
            finally:
                ready.set()
        thread = threading.Thread(target=work, daemon=True, name="github-read-only-fetch")
        thread.start()
        if not ready.wait(min(timeout, max(0, self.deadline - time.monotonic()))):
            self.poisoned = True
            raise _FetchError("DEADLINE_EXCEEDED" if time.monotonic() >= self.deadline else "TIMEOUT")
        success, value = box[0]
        if not success:
            raise value
        return value


def _positive_id(value):
    return type(value) is int and value > 0


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r"[a-fA-F0-9]{40,64}", value) is not None and len(value) in (40, 64)


def _branch(value):
    return (isinstance(value, str) and 0 < len(value) <= 255
            and re.fullmatch(r"[A-Za-z0-9_./-]+", value) is not None
            and not any(segment in ("", ".", "..") for segment in value.split("/")))


def _canonical(repository):
    if not isinstance(repository, str) or not re.fullmatch(
        r"https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)/([A-Za-z0-9_.-]{1,100})", repository
    ):
        return None
    owner, name = repository.removeprefix("https://github.com/").split("/")
    if ".." in name or name in (".", "..") or name.lower().endswith(".git"):
        return None
    return "https://github.com/" + owner + "/" + name


def _result(snapshot, problems, essential=True, *, max_chars=6500):
    result = {"status": ("UNKNOWN" if not essential else "PARTIAL" if problems else "OK"),
              "snapshot": snapshot, "problems": problems}
    if max_chars is not None and len(json.dumps(result, allow_nan=False)) > max_chars:
        problems.append({"code": "OUTPUT_TRUNCATED", "scope": "output"})
        if essential:
            result["status"] = "PARTIAL"
        compare = snapshot.get("compare")
        if isinstance(compare, dict):
            compare["summary_sha256"] = _digest(compare)
            compare["complete"] = False
            snapshot["coverage"]["compare"] = "PARTIAL"
        while len(json.dumps(result, allow_nan=False)) > max_chars:
            if isinstance(compare, dict) and compare["changed_paths"]:
                compare["changed_paths"].pop()
            elif isinstance(compare, dict) and compare["commits"]:
                compare["commits"].pop()
            else:
                facets = [snapshot.get(kind) for kind in ("comments", "reviews", "workflow_runs")]
                facets = [facet for facet in facets if isinstance(facet, dict) and facet.get("items")]
                if facets:
                    largest = max(facets, key=lambda facet: len(json.dumps(facet["items"])))
                    largest["items"].pop(0)
                    largest["items_truncated"] = True
                elif isinstance(snapshot.get("release"), dict) and snapshot["release"].get("preview"):
                    snapshot["release"]["preview"] = ""
                elif snapshot.get("preview"):
                    snapshot["preview"] = ""
                else:
                    break
    return result


def _request(fetch, path, scope, problems, absent=False):
    try:
        obj, headers = fetch(path)
    except _FetchError as error:
        problems.append({"code": error.code, "scope": scope})
        return None, {}, "UNKNOWN"
    except TimeoutError:
        problems.append({"code": "TIMEOUT", "scope": scope})
        return None, {}, "UNKNOWN"
    except OSError:
        problems.append({"code": "NETWORK_ERROR", "scope": scope})
        return None, {}, "UNKNOWN"
    except http.client.HTTPException:
        problems.append({"code": "PROTOCOL_ERROR", "scope": scope})
        return None, {}, "UNKNOWN"
    if not isinstance(headers, dict):
        problems.append({"code": "INVALID_RESPONSE", "scope": scope})
        return None, {}, "UNKNOWN"
    headers = {str(k).lower(): v for k, v in headers.items()}
    raw_status = headers.get(":status", 200)
    if type(raw_status) is not int or not 100 <= raw_status <= 599:
        problems.append({"code": "INVALID_RESPONSE", "scope": scope})
        return None, {}, "UNKNOWN"
    status = raw_status
    if status == 404 and absent:
        return None, headers, "ABSENT"
    if status != 200:
        code = "HTTP_" + str(status) if status in (301, 302, 303, 307, 308, 400, 401, 403, 404, 410, 422, 429, 500, 502, 503, 504) else "HTTP_ERROR"
        problem = {"code": code, "scope": scope, "http_status": status}
        if status in (403, 429):
            problem["rate_limit"] = {
                name: int(headers[key]) if str(headers.get(key, "")).isdigit() else None
                for name, key in (("remaining", "x-ratelimit-remaining"),
                                  ("limit", "x-ratelimit-limit"),
                                  ("reset", "x-ratelimit-reset"),
                                  ("retry_after", "retry-after"))}
        problems.append(problem)
        return None, headers, "UNKNOWN"
    return obj, headers, "OK"


def observe_repository(repository: str, previous: dict | None = None, *, fetch=None, _release_only=False, metadata_evidence=None) -> dict:
    """Observe metadata, default-branch HEAD and the latest public release."""
    repository = _canonical(repository)
    if repository is None:
        return _result({"repository": None}, [{"code": "INVALID_REPOSITORY", "scope": "repository"}], False)
    prefix = "/repos/" + repository.removeprefix("https://github.com/")
    fetch = _HTTPFetch() if fetch is None else fetch
    problems = []
    snapshot = {"repository": repository, "repository_id": None, "default_branch": None,
                "head_sha": None, "release": None, "archived": None, "license": None,
                "license_scope": "metadata_only",
                "coverage": {"metadata": "UNKNOWN", "head": "UNKNOWN", "release": "UNKNOWN"}}
    metadata, _, state, metadata_source = _metadata_input(fetch, prefix, problems, metadata_evidence)
    snapshot["coverage"]["metadata"] = state
    if metadata_source is not None:
        snapshot['metadata_source'] = metadata_source
    if state != "OK":
        return _result(snapshot, problems, False)
    if isinstance(metadata, dict) and metadata.get("private") is True:
        problems.append({"code": "NOT_PUBLIC", "scope": "metadata"})
        snapshot["coverage"]["metadata"] = "UNKNOWN"
        return _result(snapshot, problems, False)
    if (not isinstance(metadata, dict) or not _positive_id(metadata.get("id"))
            or metadata.get("private") is not False or type(metadata.get("archived")) is not bool
            or not _branch(metadata.get("default_branch"))
            or not isinstance(metadata.get("full_name"), str)
            or ("https://github.com/" + metadata["full_name"]).lower() != repository.lower()
            or (metadata.get("license") is not None and
                (not isinstance(metadata["license"], dict) or any(
                    value is not None and (not isinstance(value, str) or len(value) > 200)
                    for value in (metadata["license"].get(key) for key in ("spdx_id", "key", "name")))))):
        problems.append({"code": "INVALID_SCHEMA", "scope": "metadata"})
        snapshot["coverage"]["metadata"] = "UNKNOWN"
        return _result(snapshot, problems, False)
    snapshot["repository"] = "https://github.com/" + metadata["full_name"]
    license_value = metadata.get("license")
    license_summary = ({key: license_value.get(key) for key in ("spdx_id", "key", "name")}
                       if license_value is not None else None)
    snapshot.update(repository_id=metadata["id"], default_branch=metadata["default_branch"],
                    archived=metadata["archived"], license=license_summary)
    if _release_only:
        snapshot['kind'] = 'release'
        snapshot['coverage']['head'] = 'NOT_REQUESTED'
    else:
        head, _, state = _request(fetch, prefix + "/commits/" + quote(metadata["default_branch"], safe=""), "head", problems)
        snapshot["coverage"]["head"] = state
        if state == "OK":
            if isinstance(head, dict) and _sha(head.get("sha")):
                snapshot["head_sha"] = head["sha"]
            else:
                problems.append({"code": "INVALID_SCHEMA", "scope": "head"})
                snapshot["coverage"]["head"] = "UNKNOWN"
    release, _, state = _request(fetch, prefix + "/releases/latest", "release", problems, absent=True)
    snapshot["coverage"]["release"] = state
    if state == "OK":
        if (not isinstance(release, dict) or not _positive_id(release.get("id"))
                or not isinstance(release.get("tag_name"), str) or not 0 < len(release["tag_name"]) <= 200
                or not isinstance(release.get("published_at"), str) or len(release["published_at"]) > 80
                or (release.get("body") is not None and not isinstance(release["body"], str))):
            problems.append({"code": "INVALID_SCHEMA", "scope": "release"})
            snapshot["coverage"]["release"] = "UNKNOWN"
        else:
            body = release.get("body") or ""
            snapshot["release"] = {
                "tag": release["tag_name"], "id": release["id"], "published_at": release["published_at"],
                "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(), "preview": body[:300]}
    snapshot["compare"] = None
    snapshot["coverage"]["compare"] = "NOT_REQUESTED"
    prior = previous.get("snapshot", previous) if isinstance(previous, dict) else None
    if (isinstance(prior, dict) and _sha(prior.get("head_sha")) and snapshot["head_sha"]
            and _canonical(prior.get("repository")) is not None
            and prior["repository"].lower() == snapshot["repository"].lower()
            and prior.get("repository_id", snapshot["repository_id"]) == snapshot["repository_id"]
            and prior["head_sha"] != snapshot["head_sha"]):
        old, new = prior["head_sha"], snapshot["head_sha"]
        obj, headers, state = _request(fetch, prefix + "/compare/" + old + "..." + new + "?per_page=100&page=1", "compare", problems)
        snapshot["coverage"]["compare"] = state
        if state == "OK":
            if (not isinstance(obj, dict) or type(obj.get("total_commits")) is not int
                    or not isinstance(obj.get("commits"), list) or not isinstance(obj.get("files"), list)):
                problems.append({"code": "INVALID_SCHEMA", "scope": "compare"})
                snapshot["coverage"]["compare"] = "UNKNOWN"
            else:
                commits, paths = [], []
                valid = True
                for item in obj["commits"]:
                    if (not isinstance(item, dict) or not _sha(item.get("sha"))
                            or not isinstance(item.get("commit"), dict)
                            or not isinstance(item["commit"].get("message"), str)):
                        valid = False
                        continue
                    message = item["commit"]["message"]
                    title = (message.splitlines() or [""])[0]
                    if len(title) > 200:
                        valid = False
                    commits.append({"sha": item["sha"], "title": title[:200]})
                for item in obj["files"]:
                    if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                        valid = False
                        continue
                    filename = item["filename"]
                    if len(filename) > 300:
                        valid = False
                    paths.append(filename[:300])
                complete = (valid and obj["total_commits"] == len(obj["commits"])
                            and len(commits) <= 20 and len(paths) <= 20
                            and len(obj["files"]) < 300
                            and 'rel="next"' not in str(headers.get("link", "")))
                snapshot["compare"] = {"base_sha": old, "head_sha": new, "total_commits": obj["total_commits"],
                                       "changed_paths": paths[:20], "commits": commits[:20], "complete": complete}
                if not complete:
                    problems.append({"code": "COMPARE_TRUNCATED", "scope": "compare"})
                    snapshot["coverage"]["compare"] = "PARTIAL"
    return _result(snapshot, problems)


def observe_release(watch: dict, previous: dict | None = None, *, fetch=None, metadata_evidence=None) -> dict:
    """Observe the latest formal release without default-branch HEAD polling."""
    if not isinstance(watch, dict) or watch.get('kind') != 'release':
        return _result({'repository': None}, [{'code': 'INVALID_WATCH', 'scope': 'watch'}], False)
    return observe_repository(watch.get('repository'), previous, fetch=fetch, _release_only=True, metadata_evidence=metadata_evidence)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _activity_item(item, kind):
    if not isinstance(item, dict) or not _positive_id(item.get("id")):
        raise ValueError("invalid item")
    updated = item.get("updated_at") or item.get("submitted_at")
    if not isinstance(updated, str) or len(updated) > 80:
        raise ValueError("invalid updated_at")
    if kind == "workflow_runs":
        if (not _sha(item.get("head_sha")) or not isinstance(item.get("status"), str)
                or len(item["status"]) > 40 or type(item.get("run_attempt")) is not int
                or (item.get("conclusion") is not None and
                    (not isinstance(item["conclusion"], str) or len(item["conclusion"]) > 40))):
            raise ValueError("invalid workflow")
        return {key: item.get(key) for key in ("id", "head_sha", "status", "conclusion", "updated_at", "run_attempt")}
    if item.get("body") is not None and not isinstance(item.get("body"), str):
        raise ValueError("invalid body")
    result = {"id": item["id"], "updated_at": updated,
              "body_sha256": hashlib.sha256((item.get("body") or "").encode("utf-8")).hexdigest()}
    if kind == "reviews":
        if (not isinstance(item.get("state"), str) or len(item["state"]) > 40
                or not _sha(item.get("commit_id"))):
            raise ValueError("invalid review")
        result.update(state=item["state"], commit_id=item["commit_id"])
    return result


def _activity(fetch, path, kind, problems, *, inventory=False):
    collected = []
    complete = False
    failed = False
    total = None
    for page in range(1, 4):
        request_path = path + ("&" if "?" in path else "?") + "per_page=100&page=" + str(page)
        obj, headers, state = _request(fetch, request_path, kind, problems)
        if state != "OK":
            failed = True
            break
        values = obj.get("workflow_runs") if kind == "workflow_runs" and isinstance(obj, dict) else obj
        if kind == "workflow_runs" and isinstance(obj, dict):
            current_total = obj.get("total_count")
            if (type(current_total) is not int or current_total < 0
                    or (total is not None and current_total != total)):
                problems.append({"code": "INVALID_SCHEMA", "scope": kind})
                failed = True
                break
            total = current_total
        if not isinstance(values, list) or len(values) > 100:
            problems.append({"code": "INVALID_SCHEMA", "scope": kind})
            failed = True
            break
        try:
            page_items = [_activity_item(item, kind) for item in values]
            if kind == "workflow_runs" and any(item["head_sha"] != path.split("head_sha=", 1)[1] for item in page_items):
                raise ValueError("wrong head")
        except (ValueError, UnicodeError):
            problems.append({"code": "INVALID_SCHEMA", "scope": kind})
            failed = True
            break
        ids = [item["id"] for item in collected + page_items]
        if len(ids) != len(set(ids)) or (type(total) is int and len(ids) > total):
            problems.append({"code": "INVALID_PAGINATION", "scope": kind})
            failed = True
            break
        collected.extend(page_items)
        link = str(headers.get("link", ""))
        next_links = re.findall(r'<([^>]+)>\s*;\s*rel=[\"\']next[\"\']', link)
        if "next" in link.lower() and not next_links:
            problems.append({"code": "INVALID_PAGINATION", "scope": kind})
            failed = True
            break
        expected_next = "https://api.github.com" + path + ("&" if "?" in path else "?") + "per_page=100&page=" + str(page + 1)
        if next_links and (len(next_links) != 1 or next_links[0] != expected_next):
            problems.append({"code": "INVALID_PAGINATION", "scope": kind})
            failed = True
            break
        has_more = bool(next_links) or len(values) == 100 or (type(total) is int and len(collected) < total)
        if not has_more:
            complete = True
            break
        if page == 3:
            problems.append({"code": "PAGINATION_LIMIT", "scope": kind})
    items = sorted(collected, key=lambda item: item["id"])
    state = "OK" if complete else "PARTIAL" if items else "UNKNOWN"
    return {"items": items if inventory else items[-5:], "observed_count": len(items), "complete": complete,
            "items_truncated": not inventory and len(items) > 5, "sha256": _digest(items) if items or complete else None,
            "hash_scope": "all_observed_items", "status": "UNKNOWN" if failed else state,
            "total_count": total}, state


def _metadata_input(fetch, prefix, problems, evidence, *, number=None):
    """Public metadata uses the root GET; derived evidence is unsupported."""
    if evidence is None:
        obj, headers, state = _request(fetch, prefix, 'metadata', problems)
        return obj, headers, state, None
    problems.append({'code': 'METADATA_EVIDENCE_UNSUPPORTED', 'scope': 'metadata'})
    return None, {}, 'UNKNOWN', None


def observe_watch(watch: dict, previous: dict | None = None, *, fetch=None, metadata_evidence=None) -> dict:
    """Observe one public pull request; optional activity is added as separate facets."""
    if (not isinstance(watch, dict) or watch.get("kind") != "pull"
            or not _positive_id(watch.get("number")) or watch["number"] > 2147483647
            or any(type(watch.get(key, False)) is not bool for key in ("comments", "reviews", "workflow_runs", "activity_inventory"))):
        return _result({"repository": None}, [{"code": "INVALID_WATCH", "scope": "watch"}], False)
    repository = _canonical(watch.get("repository"))
    if repository is None:
        return _result({"repository": None}, [{"code": "INVALID_REPOSITORY", "scope": "watch"}], False)
    prefix = "/repos/" + repository.removeprefix("https://github.com/")
    fetch = _HTTPFetch() if fetch is None else fetch
    problems = []
    metadata, _, state, metadata_source = _metadata_input(fetch, prefix, problems, metadata_evidence, number=watch['number'])
    snapshot = {"repository": repository, "repository_id": None, "kind": "pull", "number": watch["number"],
                "coverage": {"metadata": state, "pull": "UNKNOWN"}}
    if metadata_source is not None:
        snapshot['metadata_source'] = metadata_source
    if state != "OK":
        return _result(snapshot, problems, False)
    if not isinstance(metadata, dict) or metadata.get("private") is not False:
        problems.append({"code": "NOT_PUBLIC", "scope": "metadata"})
        snapshot["coverage"]["metadata"] = "UNKNOWN"
        return _result(snapshot, problems, False)
    if (not _positive_id(metadata.get("id")) or not isinstance(metadata.get("full_name"), str)
            or ("https://github.com/" + metadata["full_name"]).lower() != repository.lower()):
        problems.append({"code": "INVALID_SCHEMA", "scope": "metadata"})
        snapshot["coverage"]["metadata"] = "UNKNOWN"
        return _result(snapshot, problems, False)
    snapshot["repository_id"] = metadata["id"]
    snapshot["repository"] = "https://github.com/" + metadata["full_name"]
    pull, _, state = _request(fetch, prefix + "/pulls/" + str(watch["number"]), "pull", problems)
    snapshot["coverage"]["pull"] = state
    if state != "OK":
        return _result(snapshot, problems, False)
    valid = (isinstance(pull, dict) and _positive_id(pull.get("id"))
             and type(pull.get("number")) is int and pull["number"] == watch["number"]
             and pull.get("state") in ("open", "closed") and type(pull.get("merged")) is bool
             and "merge_commit_sha" in pull
             and (pull.get("merge_commit_sha") is None or _sha(pull["merge_commit_sha"]))
             and isinstance(pull.get("updated_at"), str) and len(pull["updated_at"]) <= 80
             and (pull.get("body") is None or isinstance(pull["body"], str)))
    for side in ("head", "base"):
        part = pull.get(side) if isinstance(pull, dict) else None
        if (not isinstance(part, dict) or "repo" not in part
                or not _sha(part.get("sha")) or not _branch(part.get("ref"))):
            valid = False
            continue
        repo = part.get("repo")
        if repo is not None and (not isinstance(repo, dict) or not _positive_id(repo.get("id"))
                or repo.get("private") is not False or _canonical("https://github.com/" + str(repo.get("full_name"))) is None):
            valid = False
        if side == "base" and (not repo or repo.get("id") != snapshot["repository_id"]):
            valid = False
    if not valid:
        problems.append({"code": "INVALID_SCHEMA", "scope": "pull"})
        snapshot["coverage"]["pull"] = "UNKNOWN"
        return _result(snapshot, problems, False)
    body = pull.get("body") or ""
    snapshot.update(state=pull["state"], merged=pull["merged"], merge_commit_sha=pull["merge_commit_sha"],
                    updated_at=pull["updated_at"], body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                    preview=body[:300])
    for side in ("head", "base"):
        part = pull[side]
        repo = part["repo"]
        snapshot[side] = {"sha": part["sha"], "ref": part["ref"],
                          "repository": "https://github.com/" + repo["full_name"] if repo else None,
                          "repository_id": repo["id"] if repo else None}
    endpoints = {"comments": prefix + "/issues/" + str(watch["number"]) + "/comments",
                 "reviews": prefix + "/pulls/" + str(watch["number"]) + "/reviews",
                 "workflow_runs": prefix + "/actions/runs?head_sha=" + snapshot["head"]["sha"]}
    for kind, path in endpoints.items():
        snapshot[kind] = None
        snapshot["coverage"][kind] = "NOT_REQUESTED"
        if watch.get(kind) is True:
            snapshot[kind], snapshot["coverage"][kind] = _activity(fetch, path, kind, problems,
                                                                inventory=watch.get('activity_inventory', False))
            if kind == "workflow_runs":
                snapshot[kind]["head_sha"] = snapshot["head"]["sha"]
    # Canonical inventories are bounded by _activity's three 100-item pages,
    # field validation, and the HTTP byte/deadline budget, not the chat budget.
    # The delivery layer separately limits message locators; never delete an
    # already-observed object to make a local canonical inventory fit a chat.
    return _result(snapshot, problems, max_chars=None if watch.get('activity_inventory') else 6500)
