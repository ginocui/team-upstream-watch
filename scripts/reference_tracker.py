"""WE public-reference discovery; read only explicitly approved roots."""
from __future__ import annotations
import argparse
import copy
from contextlib import closing
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
import stat
import tomllib

GITHUB = re.compile(r'https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?=[/#?\s)\]}>\"\u0027]|$)', re.I)
PUBLIC_REPOSITORY = re.compile(r'https://(?P<host>github\.com|raw\.githubusercontent\.com|api\.github\.com/repos|huggingface\.co)/(?P<owner>[A-Za-z0-9_.-]+)/(?P<name>[A-Za-z0-9_.-]+)(?=[/#?\s)\]}>\"\u0027]|$)', re.I)


EXCLUDED = {'.git', '.obsidian', '.trash', 'private', '私域', 'credentials', 'secrets',
            'cache', 'staging', 'archive', 'archives', '归档', '历史', '__pycache__',
            'node_modules', '.venv', 'venv', 'worktrees', 'test-evidence', 'source-cache'}
SUFFIXES = {'.md', '.rst', '.txt', '.json', '.toml', '.yaml', '.yml', '.py', '.mod'}
EXCLUDED.update({'history', 'raw', 'vendor', 'vendored', 'dist', 'build', 'logs', 'sessions', 'memories',
                 'auth', '私密', '业务材料', '素材', '原始素材', 'outputs', 'output', 'jobs', 'temp', 'tmp',
                 'tests', 'fixtures', '示例', '候选版本', '旧版', 'site-packages'})
EXCLUDED.update({'versions', 'version', '版本', 'source-archive', 'source_archive', 'source_cache', 'git-snapshot'})
DENIED_FILES = ('auth.json', 'credentials*', '*secret*', '*token*', '*cookie*')


def _reparse(path):
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def discover(roots, *, scopes=None, max_file_bytes=1048576, max_files=20000):
    repositories, issues, seen, declarations = {}, [], set(), []
    dependencies, unsupported = [], []
    count = excluded = 0
    def accept(path):
        nonlocal count
        identity = os.path.normcase(os.path.abspath(path))
        if identity in seen:
            return
        seen.add(identity)
        if len(seen) > max_files:
            issues.append({'path': str(path), 'reason': 'FILE_COUNT_LIMIT'})
            return
        try:
            if _reparse(path):
                issues.append({'path': str(path), 'reason': 'REPARSE_NOT_READ'})
                return
            if path.lstat().st_nlink != 1:
                issues.append({'path': str(path), 'reason': 'HARDLINK_NOT_READ'})
                return
            with path.open('rb') as stream:
                raw = stream.read(max_file_bytes + 1)
            if len(raw) > max_file_bytes:
                issues.append({'path': str(path), 'reason': 'FILE_SIZE_LIMIT'})
                return
            text = raw.decode('utf-8-sig')
        except (OSError, UnicodeError):
            issues.append({'path': str(path), 'reason': 'INPUT_UNAVAILABLE_OR_ENCODING'})
            return
        count += 1
        digest = hashlib.sha256(raw).hexdigest()
        def package(declaration, ecosystem):
            dependencies.append({'path': str(path), 'source_sha256': digest, 'declaration': declaration,
                                 'ecosystem': ecosystem, 'repository': None, 'status': 'SOURCE_MAPPING_UNKNOWN'})
        try:
            if path.name == 'pyproject.toml':
                metadata = tomllib.loads(text)
                for entry in metadata.get('project', {}).get('dependencies', []):
                    package(entry, 'python')
                for group in metadata.get('project', {}).get('optional-dependencies', {}).values():
                    for entry in group:
                        package(entry, 'python_optional')
                for name, version in metadata.get('tool', {}).get('poetry', {}).get('dependencies', {}).items():
                    if name != 'python':
                        package(name + '@' + (version if isinstance(version, str) else json.dumps(version, sort_keys=True)), 'python_poetry')
            elif path.name == 'package.json':
                metadata = json.loads(text)
                for group in ('dependencies', 'devDependencies', 'optionalDependencies', 'peerDependencies'):
                    for name, version in metadata.get(group, {}).items():
                        package(name + '@' + str(version), 'npm_' + group)
            elif path.name == 'Cargo.toml':
                metadata = tomllib.loads(text)
                for group in ('dependencies', 'dev-dependencies', 'build-dependencies'):
                    for name, version in metadata.get(group, {}).items():
                        package(name + '@' + (version if isinstance(version, str) else json.dumps(version, sort_keys=True)), 'cargo_' + group)
            elif path.name == 'go.mod':
                in_require = False
                for line in text.splitlines():
                    entry = line.split('//', 1)[0].strip()
                    if entry == 'require (':
                        in_require = True
                    elif entry == ')':
                        in_require = False
                    elif entry.startswith('require '):
                        package(entry.removeprefix('require ').strip(), 'go')
                    elif in_require and entry:
                        package(entry, 'go')
        except (ValueError, KeyError, TypeError, AttributeError):
            issues.append({'path': str(path), 'reason': 'PACKAGE_MANIFEST_INVALID'})
        if path.suffix.casefold() == '.json':
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and obj.get('schema') == 'we_external_reference/1':
                    base = obj['baseline']
                    if not re.fullmatch('[0-9a-f]{40}', base['commit']):
                        raise ValueError('invalid declared commit')
                    repository_key(base['repository'])
                    declarations.append({'repository': base['repository'], 'baseline_commit': base['commit'],
                                         'consumer': obj.get('project'), 'usage': 'declared_reference', 'interval_hours': 24,
                                         'source_path': str(path)})
                elif isinstance(obj, dict) and obj.get('schema') == 'we.external-reference-batch.v1':
                    for base in obj['repositories']:
                        if not re.fullmatch('[0-9a-f]{40}', base['reviewed_sha']):
                            raise ValueError('invalid declared commit')
                        repository_key(base['resolved_url'])
                        declarations.append({'repository': base['resolved_url'], 'aliases': [base['original_url']],
                                             'baseline_commit': base['reviewed_sha'], 'usage': base.get('disposition', 'research'),
                                             'consumer': base.get('work_packages_context'), 'interval_hours': 168,
                                             'source_path': str(path)})
            except (ValueError, KeyError, TypeError):
                issues.append({'path': str(path), 'reason': 'STRUCTURED_RECORD_INVALID'})
        for line_number, line in enumerate(text.splitlines(), 1):
            if path.name.startswith('requirements') and path.suffix == '.txt' and line.strip() and not line.lstrip().startswith('#'):
                if not PUBLIC_REPOSITORY.search(line):
                    dependencies.append({'path': str(path), 'line': line_number, 'declaration': line.strip(), 'repository': None, 'status': 'SOURCE_MAPPING_UNKNOWN'})
            for match in re.finditer(r'https://(?:gitlab\.com|codeberg\.org|bitbucket\.org)/[^\s)\]>"\u0027]+', line):
                unsupported.append({'url': match[0], 'path': str(path), 'line': line_number, 'status': 'PROVIDER_NOT_IMPLEMENTED'})
            for match in PUBLIC_REPOSITORY.finditer(line):
                host, owner, name = match.group('host', 'owner', 'name')
                if name.endswith('.git'):
                    name = name[:-4]
                if owner in ('.', '..') or name in ('.', '..'):
                    continue
                hf = host.casefold() == 'huggingface.co'
                key = ('hf:' if hf else '') + (owner + '/' + name).casefold()
                row = repositories.setdefault(key, {'key': key, 'repository': ('https://huggingface.co/' if hf else 'https://github.com/') + owner + '/' + name, 'evidence': []})
                row['evidence'].append({'path': str(path), 'line': line_number, 'sha256': digest})
    source_scopes = [{'path': str(p)} for p in roots] + list(scopes or [])
    for scope in source_scopes:
        root = Path(scope['path'])
        if not root.is_absolute() or not root.exists():
            issues.append({'path': str(root), 'reason': 'ROOT_UNAVAILABLE'})
            continue
        if _reparse(root):
            issues.append({'path': str(root), 'reason': 'REPARSE_NOT_READ'})
            continue
        if root.is_file():
            accept(root)
            continue
        def onerror(error):
            issues.append({'path': str(root), 'reason': 'DIRECTORY_UNAVAILABLE'})
        for directory, dirs, files in os.walk(root, followlinks=False, onerror=onerror):
            allowed = []
            for name in sorted(dirs):
                path = Path(directory) / name
                if name.casefold() in EXCLUDED or name.startswith('.'):
                    excluded += 1
                elif _reparse(path):
                    issues.append({'path': str(path), 'reason': 'REPARSE_NOT_READ'})
                else:
                    allowed.append(name)
            depth = len(Path(directory).relative_to(root).parts)
            dirs[:] = allowed if depth < scope.get('max_depth', 100) else []
            for name in sorted(files):
                relative = (Path(directory) / name).relative_to(root).as_posix()
                include = scope.get('include', ['*'])
                included = any(fnmatch.fnmatchcase(relative, pattern) or (pattern.startswith('**/') and fnmatch.fnmatchcase(relative, pattern[3:])) for pattern in include)
                if Path(name).suffix.casefold() in SUFFIXES and not name.startswith('.') and included and not any(fnmatch.fnmatchcase(name.casefold(), p) for p in DENIED_FILES):
                    accept(Path(directory) / name)
            if len(seen) > max_files:
                dirs[:] = []
                break
    return {'repositories': [repositories[k] for k in sorted(repositories)], 'issues': issues, 'declared_seeds': declarations,
            'unresolved_dependencies': dependencies, 'unsupported_sources': unsupported,
            'coverage': {'files_read': count, 'files_considered': len(seen), 'excluded_directories': excluded,
                         'complete': not issues, 'upstream_support_complete': not (dependencies or unsupported),
                         'permitted_roots': [s['path'] for s in source_scopes]}}


def repository_key(url):
    match = PUBLIC_REPOSITORY.fullmatch(url)
    if match is None:
        raise ValueError('REPOSITORY_URL_INVALID')
    host, owner, name = match.group('host', 'owner', 'name')
    if name.endswith('.git'):
        name = name[:-4]
    if owner in ('.', '..') or name in ('.', '..'):
        raise ValueError('REPOSITORY_URL_INVALID')
    return ('hf:' if host.casefold() == 'huggingface.co' else '') + (owner + '/' + name).casefold()


def reconcile(discovery, seeds, previous):
    rows = copy.deepcopy(previous)
    seeds = [*discovery.get('declared_seeds', []), *seeds]
    aliases = {}
    for seed in seeds:
        key = repository_key(seed['repository'])
        for url in [seed['repository'], *seed.get('aliases', [])]:
            alias = repository_key(url)
            if alias in aliases and aliases[alias] != key:
                raise ValueError('ALIAS_CONFLICT')
            aliases[alias] = key
    for row in rows.values():
        row['discovery_status'] = 'NO_LONGER_REFERENCED' if discovery['coverage']['complete'] else 'DISCOVERY_UNKNOWN'
        if discovery['coverage']['complete']:
            row['evidence'] = []
    for seed in seeds:
        key = repository_key(seed['repository'])
        row = rows.setdefault(key, {'key': key, 'repository': seed['repository'], 'baseline_commit': seed.get('baseline_commit'),
                                     'last_observation': None, 'adoption_changed': False, 'evidence': []})
        row.update(usage=seed.get('usage', 'unclassified_reference'), consumer=seed.get('consumer'),
                   interval_hours=seed.get('interval_hours', 168), aliases=seed.get('aliases', []),
                   discovery_status='REFERENCED')
        if seed.get('baseline_commit') and row['baseline_commit'] != seed['baseline_commit']:
            row.update(declared_baseline_commit=seed['baseline_commit'], baseline_review_pending=True)
    for found in discovery['repositories']:
        key = aliases.get(found['key'], found['key'])
        row = rows.setdefault(key, {'key': key, 'repository': found['repository'], 'baseline_commit': None,
                                    'last_observation': None, 'adoption_changed': False, 'evidence': [],
                                    'usage': 'unclassified_reference', 'consumer': None, 'interval_hours': 168, 'aliases': []})
        row['evidence'].extend(found['evidence'])
        row['discovery_status'] = 'REFERENCED'
    return {k: rows[k] for k in sorted(rows)}


def is_due(row, now):
    checked = row.get('checked_at')
    if checked is None:
        return True
    schedule = row.get('schedule')
    if schedule:
        if schedule.get('kind') != 'weekly':
            raise ValueError('UNSUPPORTED_SCHEDULE')
        current = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
        slot = current.replace(hour=schedule['hour'], minute=schedule.get('minute', 0), second=0, microsecond=0)
        slot -= timedelta(days=(current.weekday() - schedule['weekday']) % 7)
        if slot.timestamp() > now:
            slot -= timedelta(days=7)
        return checked < slot.timestamp()
    return now - checked >= row.get('interval_hours', 168) * 3600


def observation_identity(observation):
    stable = copy.deepcopy(observation)
    snapshot = stable.get('snapshot')
    if isinstance(snapshot, dict):
        snapshot.pop('metadata_source', None)  # Acquisition IDs/headers are provenance, not an upstream change.
        snapshot.pop('comparison', None)
        snapshot.pop('compare', None)
        if isinstance(snapshot.get('coverage'), dict):
            snapshot['coverage'].pop('compare', None)
    return hashlib.sha256(json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def check_due(rows, now, observe, *, max_checks=40, force=False, max_seconds=120, clock=time.monotonic, run_id=None):
    result, events = copy.deepcopy(rows), []
    end = clock() + max_seconds
    due = [r for r in result.values() if r['discovery_status'] == 'REFERENCED' and not r.get('migration_hold') and
           (force or is_due(r, now))]
    due.sort(key=lambda r: (r.get('checked_at', float('-inf')), r['key']))
    for row in due[:max_checks]:
        if clock() >= end:
            break
        previous = row.get('last_observation')
        try:
            observation = observe(row['repository'], row.get('last_good_snapshot'))
            if observation.get('status') not in ('OK', 'PARTIAL', 'UNKNOWN'):
                raise ValueError('OBSERVATION_SHAPE')
        except Exception as exc:
            observation = {'status': 'UNKNOWN', 'snapshot': None, 'problems': [{'code': 'SOURCE_EXCEPTION', 'type': type(exc).__name__}]}
        fingerprint = observation_identity(observation)
        before = row.get('observation_fingerprint')
        if fingerprint != before:
            if observation['status'] == 'UNKNOWN':
                kind = 'OBSERVATION_UNKNOWN'
            elif observation['status'] == 'PARTIAL':
                kind = 'OBSERVATION_PARTIAL'
            elif previous and previous['status'] in ('UNKNOWN', 'PARTIAL'):
                kind = 'OBSERVATION_RECOVERED'
            else:
                kind = 'FIRST_OBSERVATION' if previous is None else 'UPSTREAM_CHANGED'
            events.append({'event': kind, 'key': row['key'], 'repository': row['repository'],
                           'status': observation['status'], 'baseline_commit': row.get('baseline_commit'),
                           'snapshot': observation.get('snapshot'), 'problems': observation.get('problems', []),
                           'consumer': row.get('consumer'), 'usage': row.get('usage'), 'adoption_changed': False})
        row.update(last_observation=observation, observation_fingerprint=fingerprint, checked_at=now)
        if run_id is not None:
            row['observation_run_id'] = run_id
        if observation['status'] == 'OK':
            row['last_good_snapshot'] = observation['snapshot']
            row['last_good_checked_at'] = now
            if run_id is not None:
                row['last_good_run_id'] = run_id
    return result, events


def _watch_definition_hash(watch):
    identity = {k: v for k, v in watch.items()
                if k not in ('interval_hours', 'schedule', 'consumer', 'delivery', 'id')}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _consumer_binding_hash(config, view, watch):
    identity = {'watch_id': watch['id'], 'definition_hash': _watch_definition_hash(watch),
                'consumer': view['consumer'], 'delivery': watch.get('delivery', 'local'),
                'target': os.path.normcase(os.path.abspath(Path(config['state_root']) / view['relative_root'])),
                'accepted_receipt': view['accepted_receipt']}
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def _consumer_view_targets(config):
    state = Path(config['state_root'])
    watches = {watch['id']: watch for watch in config.get('watches', [])}
    targets = []
    seen = set()
    for view in config.get('consumer_views', []):
        watch = watches.get(view.get('watch_id'))
        relative = view.get('relative_root', '')
        parts = relative.split('/') if isinstance(relative, str) else []
        if (not parts or parts[0] != 'consumers' or len(parts) < 3 or
                any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', p) for p in parts) or
                not watch or watch.get('consumer') != view.get('consumer') or
                watch.get('delivery', 'local') != 'local' or
                not isinstance(view.get('accepted_receipt'), str) or not view['accepted_receipt'].strip()):
            raise ValueError('CONSUMER_VIEW_NOT_ACCEPTED_LOCAL_TARGET')
        directory = state.joinpath(*parts)
        key = os.path.normcase(str(directory.absolute()))
        if key in seen:
            raise ValueError('DUPLICATE_CONSUMER_VIEW')
        seen.add(key)
        for parent in [directory, *directory.parents]:
            if parent.exists() and (_reparse(parent) or not parent.is_dir()):
                raise ValueError('CONSUMER_VIEW_REPARSE')
        target = directory / 'latest.json'
        if target.exists() and (_reparse(target) or target.stat().st_nlink != 1 or not target.is_file()):
            raise ValueError('CONSUMER_VIEW_UNSAFE_FILE')
        targets.append((view, watch, target))
    return targets


def project_consumer_views(config):
    targets = _consumer_view_targets(config)
    if not targets:
        return []
    database = Path(config['state_root']) / 'tracking.sqlite3'
    if not database.exists():
        return []
    if _reparse(database) or database.stat().st_nlink != 1 or not database.is_file():
        raise ValueError('CONSUMER_VIEW_UNSAFE_DATABASE')
    with closing(sqlite3.connect(database, timeout=0, isolation_level=None)) as conn:
        # Serialize with collection and other publishers. Always project the newest committed
        # run, not the caller's earlier report; a delayed publisher cannot overwrite newer data.
        conn.execute('BEGIN IMMEDIATE')
        latest = conn.execute('SELECT payload FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        if latest is None:
            conn.execute('COMMIT')
            return []
        report = json.loads(latest[0])
        # Validate every target before the first filesystem effect. A newer committed
        # report is usable only under the exact source and consumer acceptance binding.
        for view, watch, target in targets:
            row = report.get('watches', {}).get(view['watch_id'], {})
            if (row.get('discovery_status') != 'REFERENCED'
                    or row.get('definition_hash') != _watch_definition_hash(watch)
                    or row.get('repository') != watch['repository']
                    or row.get('consumer') != view['consumer']
                    or row.get('delivery') != 'local'
                    or _consumer_binding_hash(config, view, watch) not in row.get('consumer_bindings', [])):
                raise ValueError('CONSUMER_VIEW_STALE_BINDING')
        pending = [{'id': row[0], 'run_id': row[1], 'event': json.loads(row[2])}
                   for row in conn.execute('SELECT id,run_id,payload FROM review_outbox WHERE receipt IS NULL ORDER BY rowid')]
        written = []
        for view, watch, target in targets:
            legacy = watch.get('legacy_observation') or {}
            source_row = report.get('watches', {}).get(view['watch_id'], {})
            current = source_row.get('last_observation') or {}
            checked_in_run = source_row.get('observation_run_id') == report['run_id']
            as_of = datetime.fromisoformat(report['observed_at']).timestamp()
            if source_row.get('migration_hold'):
                freshness = 'MIGRATION_HOLD'
            elif source_row.get('checked_at') is None:
                freshness = 'NEVER_CHECKED'
            elif checked_in_run:
                freshness = 'CHECKED_IN_RUN'
            else:
                freshness = 'DUE_UNCHECKED' if is_due(source_row, as_of) else 'NOT_DUE_CACHED'
            snapshot = current.get('snapshot') or {}
            before_refs, after_refs = legacy.get('refs'), snapshot.get('refs')
            difference = {'status': 'UNKNOWN', 'changed_refs': None, 'compare_url': None,
                          'observation_checked_at': source_row.get('checked_at'), 'freshness': freshness,
                          'not_adoption_or_handled_review': True}
            if current.get('status') == 'OK' and isinstance(before_refs, dict) and isinstance(after_refs, dict):
                difference.update(status='KNOWN_PUBLIC_REFS_ONLY', changed_refs=[
                    {'ref': ref, 'before': before_refs.get(ref), 'after': after_refs.get(ref)}
                    for ref in sorted(set(before_refs) | set(after_refs)) if before_refs.get(ref) != after_refs.get(ref)])
                old_head, new_head = legacy.get('head_sha'), snapshot.get('head_sha')
                if all(isinstance(sha, str) and re.fullmatch('[a-f0-9]{40}', sha) for sha in (old_head, new_head)):
                    difference['compare_url'] = watch['repository'] + '/compare/' + old_head + '...' + new_head
            payload = {'schema': 'we-reference-local-consumer-view/1', 'watch_id': view['watch_id'],
                       'consumer': view['consumer'], 'accepted_receipt': view['accepted_receipt'],
                       'run_id': report['run_id'], 'observed_at': report['observed_at'],
                       'projection_as_of': report['observed_at'],
                       'checked_at': source_row.get('checked_at'),
                       'last_good_checked_at': source_row.get('last_good_checked_at'),
                       'observation_run_id': source_row.get('observation_run_id'),
                       'last_good_run_id': source_row.get('last_good_run_id'),
                       'checked_in_run': checked_in_run, 'freshness': freshness,
                       'migration_hold': source_row.get('migration_hold', False),
                       'legacy_observation': watch.get('legacy_observation'), 'since_legacy_baseline': difference,
                       'last_observation': report.get('watches', {}).get(view['watch_id'], {}).get('last_observation'),
                       'last_good_snapshot': report.get('watches', {}).get(view['watch_id'], {}).get('last_good_snapshot'),
                       'pending_events': [event for event in pending
                                          if event['event'].get('watch_id') == view['watch_id']
                                          and event['event'].get('repository') == watch['repository']
                                          and event['event'].get('definition_hash') == _watch_definition_hash(watch)
                                          and event['event'].get('consumer') == view['consumer']
                                          and event['event'].get('delivery') == 'local'
                                          and _consumer_binding_hash(config, view, watch)
                                          in event['event'].get('consumer_bindings', [])],
                       'member_has_read_or_adopted': False, 'delivery': 'local',
                       'canonical_state': 'tracking.sqlite3; this file is an as-of projection, never a delivery/adoption receipt'}
            target.parent.mkdir(parents=True, exist_ok=True)
            # Recheck after mkdir, before opening a new private sibling and replacing the view.
            _consumer_view_targets(config)
            temporary = target.with_name('.latest-' + uuid.uuid4().hex + '.json')
            try:
                with temporary.open('x', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False, indent=2)
                    stream.write('\n')
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
            written.append(str(target))
        conn.execute('COMMIT')
        return written


def run_tracking(config, *, now=None, observe=None, observe_watch=None):
    if config.get('managed_github') is not None:
        raise ValueError('MANAGED_GITHUB_UNSUPPORTED')
    targets = _consumer_view_targets(config)
    consumer_bindings = {}
    for view, watch, target in targets:
        consumer_bindings.setdefault(view['watch_id'], []).append(_consumer_binding_hash(config, view, watch))
    now = time.time() if now is None else now
    state = Path(config['state_root'])
    roots = [Path(p) for p in config.get('discovery_roots', [])]
    all_roots = roots + [Path(s['path']) for s in config.get('discovery_scopes', [])]
    if not state.is_absolute() or any(state == p or state.is_relative_to(p) for p in all_roots):
        raise ValueError('STATE_INSIDE_INPUT_OR_NOT_ABSOLUTE')
    for parent in [state, *state.parents]:
        if parent.exists() and _reparse(parent):
            raise ValueError('STATE_REPARSE')
    state.mkdir(parents=True, exist_ok=True)
    if _reparse(state):
        raise ValueError('STATE_REPARSE')
    if observe is None:
        observe = observe_source
    database = state / 'tracking.sqlite3'
    if database.exists() and (_reparse(database) or database.stat().st_nlink != 1):
        raise ValueError('STATE_REPARSE_OR_HARDLINK')
    conn = sqlite3.connect(database, timeout=0, isolation_level=None)
    try:
        try:
            conn.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            raise RuntimeError('TRACKING_BUSY') from exc
        conn.execute('CREATE TABLE IF NOT EXISTS tracking_state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, observed_at REAL NOT NULL, payload TEXT NOT NULL)')
        conn.execute('CREATE TABLE IF NOT EXISTS review_outbox (id TEXT PRIMARY KEY, run_id TEXT NOT NULL, payload TEXT NOT NULL, receipt TEXT)')
        previous = conn.execute('SELECT payload FROM tracking_state WHERE id=1').fetchone()
        before = json.loads(previous[0]) if previous else {'repositories': {}}
        discovery = discover(roots, scopes=config.get('discovery_scopes'))
        resolved_dependencies, unresolved_dependencies, package_seeds = [], [], []
        for dependency in discovery['unresolved_dependencies']:
            name = re.match(r'([A-Za-z0-9][A-Za-z0-9._-]*)', dependency['declaration'])
            package = re.sub('[-_.]+', '-', name[1]).lower() if name else None
            binding = config.get('package_sources', {}).get(package)
            if not binding or not binding.get('evidence'):
                unresolved_dependencies.append(dependency)
                continue
            repository_key(binding['repository'])
            resolved_dependencies.append(dict(dependency, repository=binding['repository'], status='SOURCE_BOUND_METADATA_ONLY',
                                               binding_evidence=binding['evidence']))
            package_seeds.append({'repository': binding['repository'], 'baseline_commit': None,
                                  'usage': 'direct_dependency', 'consumer': binding.get('consumer'), 'interval_hours': 24})
        discovery['unresolved_dependencies'] = unresolved_dependencies
        discovery['declared_seeds'].extend(package_seeds)
        discovery['coverage']['upstream_support_complete'] = not (unresolved_dependencies or discovery['unsupported_sources'])
        rows = reconcile(discovery, config.get('seeds', []), before['repositories'])
        holds = {repository_key(url) for url in config.get('observation_holds', [])}
        for key, row in rows.items():
            row['migration_hold'] = key in holds
        specialised = {repository_key(w['repository']) for w in config.get('watches', []) if w['kind'] in ('git_refs', 'atom', 'repository')}
        ordinary = {key: row for key, row in rows.items() if key not in specialised}
        events = []
        watch_rows = copy.deepcopy(before.get('watches', {}))
        definitions = {}
        for watch in config.get('watches', []):
            key = watch['id']
            if key in definitions:
                raise ValueError('DUPLICATE_WATCH_ID')
            definitions[key] = watch
            row = watch_rows.setdefault(key, {'key': key, 'repository': watch['repository'], 'baseline_commit': None,
                                             'last_observation': None, 'adoption_changed': False, 'evidence': []})
            identity_hash = _watch_definition_hash(watch)
            if row.get('definition_hash') not in (None, identity_hash):
                row.update(last_observation=None, last_good_snapshot=None)
                for field in ('checked_at', 'observation_fingerprint', 'observation_run_id',
                              'last_good_checked_at', 'last_good_run_id'):
                    row.pop(field, None)
            row.update(discovery_status='REFERENCED', interval_hours=watch.get('interval_hours', 168),
                       consumer=watch.get('consumer'), usage=watch.get('kind'), delivery=watch.get('delivery', 'local'),
                       migration_hold=watch.get('migration_hold', False),
                       repository=watch['repository'], schedule=watch.get('schedule'), definition_hash=identity_hash,
                       consumer_bindings=consumer_bindings.get(key, []))
        for key, row in watch_rows.items():
            if key not in definitions:
                row['discovery_status'] = 'NO_LONGER_REFERENCED'
                row['consumer_bindings'] = []
        if definitions and observe_watch is None:
            observe_watch = observe_specific_watch
        run_id = str(uuid.uuid4())
        queue = [('repo', key, row) for key, row in ordinary.items()]
        queue.extend(('watch', key, watch_rows[key]) for key in definitions)
        queue = [item for item in queue if item[2]['discovery_status'] == 'REFERENCED' and not item[2].get('migration_hold') and is_due(item[2], now)]
        queue.sort(key=lambda item: (item[2].get('checked_at', float('-inf')), 0 if item[0] == 'watch' else 1, item[1]))
        deadline = time.monotonic() + config.get('max_seconds', 120)
        for kind, key, row in queue[:config.get('max_checks', 40)]:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            observer = observe if kind == 'repo' else lambda repository, previous, definition=definitions[key]: observe_watch(definition, previous)
            selected, changes = check_due({key: row}, now, observer, max_checks=1, max_seconds=remaining, clock=time.monotonic, run_id=run_id)
            if kind == 'repo':
                rows[key] = selected[key]
                events.extend(changes)
            else:
                watch_rows[key] = selected[key]
                events.extend(dict(event, watch_id=key, delivery=definitions[key].get('delivery', 'local'),
                                   definition_hash=watch_rows[key]['definition_hash'],
                                   consumer_bindings=consumer_bindings.get(key, [])) for event in changes)
        for key in sorted(watch_rows):
            if key not in definitions:
                continue
            repo_key = repository_key(definitions[key]['repository'])
            if definitions[key]['kind'] in ('atom', 'git_refs', 'repository') and repo_key in rows:
                source = rows[repo_key]
                for field in ('last_observation', 'last_good_snapshot', 'checked_at', 'observation_fingerprint'):
                    if field in watch_rows[key]:
                        source[field] = copy.deepcopy(watch_rows[key][field])
                source['observation_source'] = 'watch:' + key
        report = {'schema': 'we-reference-tracking-run/1', 'run_id': run_id,
                  'observed_at': datetime.fromtimestamp(now, timezone.utc).isoformat(),
                  'coverage': discovery['coverage'], 'discovery_issues': discovery['issues'],
                  'unresolved_dependencies': discovery['unresolved_dependencies'], 'unsupported_sources': discovery['unsupported_sources'],
                  'resolved_dependencies': resolved_dependencies,
                  'counts': {'referenced_repositories': sum(r['discovery_status'] == 'REFERENCED' for r in rows.values()),
                             'tracked_historical': len(rows), 'events': len(events),
                             'unknown': sum((r.get('last_observation') or {}).get('status') == 'UNKNOWN' for r in rows.values()),
                             'never_observed': sum(r.get('last_observation') is None for r in rows.values())},
                  'repositories': rows, 'watches': watch_rows, 'events': events, 'adoption_changed': False, 'installation_performed': False}
        report['counts']['events'] = len(events)
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        conn.execute('INSERT INTO runs VALUES (?, ?, ?)', (run_id, now, payload))
        for event in events:
            event_id = str(uuid.uuid4())
            conn.execute('INSERT INTO review_outbox VALUES (?, ?, ?, NULL)',
                         (event_id, run_id, json.dumps(event, ensure_ascii=False, sort_keys=True)))
        conn.execute('INSERT INTO tracking_state VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                     (json.dumps({'repositories': rows, 'watches': watch_rows}, ensure_ascii=False, sort_keys=True),))
        conn.execute('COMMIT')
        runs = state / 'runs'
        runs.mkdir(exist_ok=True)
        with (runs / (run_id + '.json')).open('x', encoding='utf-8') as stream:
            stream.write(payload)
        project_consumer_views(config)
        return report
    except Exception:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def pending_reviews(config):
    path = Path(config['state_root']) / 'tracking.sqlite3'
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path, timeout=0)) as conn:
        return [{'id': r[0], 'run_id': r[1], 'event': json.loads(r[2])}
                for r in conn.execute('SELECT id, run_id, payload FROM review_outbox WHERE receipt IS NULL ORDER BY rowid')]


def acknowledge_review(config, event_id, receipt):
    if not isinstance(receipt, str) or not receipt.strip():
        raise ValueError('RECEIPT_REQUIRED')
    with closing(sqlite3.connect(Path(config['state_root']) / 'tracking.sqlite3', timeout=0)) as conn:
        row = conn.execute('SELECT receipt FROM review_outbox WHERE id=?', (event_id,)).fetchone()
        if row is None:
            raise ValueError('UNKNOWN_EVENT_ID')
        if row[0] not in (None, receipt):
            raise ValueError('RECEIPT_CONFLICT')
        conn.execute('UPDATE review_outbox SET receipt=? WHERE id=?', (receipt, event_id))
        conn.commit()


def render_summary(report):
    visible = [e for e in report['events'] if e.get('delivery') != 'local']
    if not visible:
        return ''
    rows = ['WE开源引用跟踪：本轮有变化或待核事项（没有自动升级/采用）。']
    names = {'FIRST_OBSERVATION': '首次观察', 'UPSTREAM_CHANGED': '上游变化',
             'OBSERVATION_UNKNOWN': '本次未知', 'OBSERVATION_PARTIAL': '部分覆盖待核', 'OBSERVATION_RECOVERED': '观察恢复'}
    for event in visible[:12]:
        rows.append('- ' + names.get(event['event'], event['event']) + '：' + event['repository'] + ' [' + event['status'] + ']')
    if len(visible) > 12:
        rows.append('其余项目见本轮原始报告；未在此展开。')
    return '\n'.join(rows)


def observe_source(repository, previous=None):
    if repository.startswith('https://huggingface.co/'):
        from reference_watch_sources import observe_huggingface
        return observe_huggingface(repository, previous)
    from github_reference_source import observe_repository
    observed = observe_repository(repository, previous)
    unavailable = any(p.get('code') in ('HTTP_429', 'TIMEOUT', 'NETWORK_ERROR') or
                      (p.get('code') == 'HTTP_403' and (p.get('rate_limit') or {}).get('remaining') == 0)
                      for p in observed.get('problems', []))
    if observed['status'] == 'UNKNOWN' and unavailable:
        from reference_watch_sources import observe_public_fallback
        fallback = observe_public_fallback(repository, previous)
        if fallback.get('snapshot') and fallback['status'] in ('OK', 'PARTIAL'):
            fallback['status'] = 'PARTIAL'
            fallback['snapshot']['observation_route'] = 'PUBLIC_GIT_HEAD_AND_RELEASE_ATOM'
            fallback['snapshot'].setdefault('coverage', {})['api_metadata'] = 'UNKNOWN'
            fallback['problems'] = [{'code': 'API_COVERAGE_UNAVAILABLE', 'scope': 'api_metadata'}] + fallback.get('problems', [])
            return fallback
    return observed


def observe_specific_watch(watch, previous=None):
    if watch['kind'] == 'git_refs':
        from reference_watch_sources import observe_git_refs
        return observe_git_refs(watch, previous)
    if watch['kind'] == 'atom':
        from reference_watch_sources import observe_atom
        return observe_atom(watch, previous)
    if watch['kind'] == 'release':
        from github_reference_source import observe_release
        return observe_release(watch, previous)
    if watch['kind'] == 'repository':
        return observe_source(watch['repository'], previous)
    from github_reference_source import observe_watch
    return observe_watch(watch, previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['discover', 'tick', 'pending', 'ack'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--summary', action='store_true')
    parser.add_argument('--event-id')
    parser.add_argument('--receipt')
    args = parser.parse_args(argv)
    try:
        path = Path(args.config)
        if not path.is_absolute():
            raise ValueError('CONFIG_ABSOLUTE_REQUIRED')
        raw = path.read_bytes()
        if len(raw) > 1048576:
            raise ValueError('CONFIG_TOO_LARGE')
        config = json.loads(raw)
        if args.action == 'tick' and not args.allow_public_network:
            raise ValueError('PUBLIC_NETWORK_NOT_ENABLED')
        if args.action == 'discover':
            result = discover(config.get('discovery_roots', []), scopes=config.get('discovery_scopes'))
        elif args.action == 'pending':
            result = pending_reviews(config)
        elif args.action == 'ack':
            acknowledge_review(config, args.event_id, args.receipt)
            result = {'acknowledged': args.event_id, 'receipt': args.receipt}
        else:
            result = run_tracking(config)
        if args.summary and args.action == 'tick':
            text = render_summary(result)
            if text:
                print(text)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
