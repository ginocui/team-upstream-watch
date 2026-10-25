"""One public-source observation shared by local team review inboxes.

Member names route data; they are not authenticated identities. This CLI never
installs upstream code, contacts team members or changes adoption baselines.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import sys

_SCRIPTS = Path(__file__).resolve().parent / 'scripts'
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
_SPEC = importlib.util.spec_from_file_location(
    'team_upstream_watch_core', Path(__file__).resolve().parent / 'scripts/reference_tracker.py')
_TRACKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_TRACKER)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('CONFIG_DUPLICATE_KEY')
        result[key] = value
    return result


def load_config(path):
    """Resolve the small team config without discovering unrelated directories."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 1048576:
        raise ValueError('CONFIG_INVALID_SIZE')
    data = json.loads(raw, object_pairs_hook=_unique_object)
    allowed = {'schema', 'state_root', 'sources', 'members', 'max_checks', 'max_seconds'}
    required = {'schema', 'state_root', 'sources', 'members'}
    if (not isinstance(data, dict) or not required <= set(data) <= allowed
            or data['schema'] != 'team-upstream-watch/1'
            or not isinstance(data['state_root'], str) or not data['state_root'].strip()
            or '\x00' in data['state_root']
            or not isinstance(data['sources'], list) or not 1 <= len(data['sources']) <= 200
            or not isinstance(data['members'], list) or not 1 <= len(data['members']) <= 200
            or type(data.get('max_checks', 1)) is not int
            or not 1 <= data.get('max_checks', 1) <= 40
            or type(data.get('max_seconds', 90)) is not int
            or not 1 <= data.get('max_seconds', 90) <= 120):
        raise ValueError('CONFIG_INVALID')
    source_ids = set()
    source_keys = {'id', 'repository', 'kind', 'interval_hours', 'number', 'branch',
                   'comments', 'reviews', 'workflow_runs', 'activity_inventory'}
    for source in data['sources']:
        if isinstance(source, dict) and 'migration_hold' in source:
            raise ValueError('CONFIG_UNSUPPORTED_MIGRATION_HOLD')
        if (not isinstance(source, dict) or not {'id', 'repository', 'kind'} <= set(source) <= source_keys
                or not isinstance(source['id'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', source['id'])
                or source['id'] in source_ids
                or source['kind'] not in ('repository', 'release', 'pull', 'git_refs', 'atom')):
            raise ValueError('CONFIG_INVALID_SOURCE')
        repository = source['repository']
        if (not isinstance(repository, str)
                or re.fullmatch(r'https://(?:github\.com|huggingface\.co)/[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*', repository) is None
                or '..' in repository.split('/')[-1]
                or repository.endswith('.git')
                or (repository.startswith('https://huggingface.co/') and source['kind'] != 'repository')):
            raise ValueError('CONFIG_INVALID_REPOSITORY')
        _TRACKER.repository_key(repository)
        if source['kind'] in ('repository', 'release', 'pull') and repository.startswith('https://github.com/'):
            from github_reference_source import _canonical
            if _canonical(repository) is None:
                raise ValueError('CONFIG_INVALID_REPOSITORY')
        if 'branch' in source and (source['kind'] != 'atom'
                                  or not isinstance(source['branch'], str)
                                  or re.fullmatch('[A-Za-z0-9_-]+', source['branch']) is None):
            raise ValueError('CONFIG_INVALID_BRANCH')
        if source['kind'] != 'pull' and any(key in source for key in ('number', 'comments', 'reviews', 'workflow_runs', 'activity_inventory')):
            raise ValueError('CONFIG_INVALID_PULL_FIELDS')
        if (type(source.get('interval_hours', 168)) not in (int, float)
                or not 0 < source.get('interval_hours', 168) <= 8760):
            raise ValueError('CONFIG_INVALID_INTERVAL')
        for flag in ('comments', 'reviews', 'workflow_runs', 'activity_inventory'):
            if flag in source and type(source[flag]) is not bool:
                raise ValueError('CONFIG_INVALID_FLAG')
        if source['kind'] == 'pull' and (type(source.get('number')) is not int
                                         or not 1 <= source['number'] <= 999999999):
            raise ValueError('CONFIG_INVALID_PULL_NUMBER')
        source_ids.add(source['id'])
    members = set()
    for member in data['members']:
        if (not isinstance(member, dict) or set(member) != {'id', 'sources'}
                or not isinstance(member['id'], str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', member['id'])
                or member['id'] in members or not isinstance(member['sources'], list)
                or any(not isinstance(item, str) or item not in source_ids for item in member['sources'])
                or len(member['sources']) != len(set(member['sources']))):
            raise ValueError('CONFIG_INVALID_MEMBER')
        members.add(member['id'])
    state = Path(data['state_root'])
    if not state.is_absolute():
        state = path.parent / state
    return {'state_root': str(state.absolute()), 'sources': data['sources'],
            'members': data['members'], 'max_checks': data.get('max_checks', 1),
            'max_seconds': data.get('max_seconds', 90)}


def _database_path(config):
    """Apply the collector's existing state/reparse/hardlink policy without mkdir."""
    state = Path(config['state_root'])
    if not state.is_absolute():
        raise ValueError('STATE_INSIDE_INPUT_OR_NOT_ABSOLUTE')
    for parent in (state, *state.parents):
        if parent.exists() and _TRACKER._reparse(parent):
            raise ValueError('STATE_REPARSE')
    database = state / 'tracking.sqlite3'
    if database.exists() and (_TRACKER._reparse(database) or database.stat().st_nlink != 1):
        raise ValueError('STATE_REPARSE_OR_HARDLINK')
    return database


def collect(config, *, now=None, observe_watch=None):
    """Delegate observation/persistence to the original collector once per source."""
    core = {'state_root': config['state_root'], 'discovery_roots': [],
            'watches': [dict(source, consumer='team', delivery='local')
                        for source in config['sources']],
            'max_checks': config['max_checks'], 'max_seconds': config['max_seconds']}
    report = _TRACKER.run_tracking(core, now=now, observe_watch=observe_watch)
    with closing(sqlite3.connect(_database_path(config).as_uri() + '?mode=rw', uri=True)) as conn:
        conn.execute('CREATE TABLE IF NOT EXISTS team_receipts '
                     '(member TEXT NOT NULL, event_id TEXT NOT NULL, receipt TEXT NOT NULL, '
                     'PRIMARY KEY(member,event_id))')
        conn.commit()
    return report


def inbox(config, member):
    """Read only this member's current configured sources and pending events."""
    match = next((row for row in config['members'] if row['id'] == member), None)
    if match is None:
        raise ValueError('UNKNOWN_MEMBER')
    database = _database_path(config)
    if not database.is_file():
        return {'member': member, 'events': [], 'adoption_changed': False}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
        conn.execute('BEGIN')
        has_receipts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_receipts'").fetchone() is not None
        latest = conn.execute('SELECT payload FROM runs ORDER BY rowid DESC LIMIT 1').fetchone()
        report = json.loads(latest[0]) if latest is not None else {}
        source_rows = []
        for definition in config['sources']:
            if definition['id'] not in match['sources']:
                continue
            observed = report.get('watches', {}).get(definition['id'], {})
            same = (observed.get('repository') == definition['repository']
                    and observed.get('definition_hash') == _TRACKER._watch_definition_hash(
                        dict(definition, consumer='team', delivery='local')))
            source_rows.append({'id': definition['id'], 'repository': definition['repository'],
                                'checked_at': observed.get('checked_at') if same else None,
                                'last_good_checked_at': observed.get('last_good_checked_at') if same else None,
                                'last_observation': observed.get('last_observation') if same else None,
                                'migration_hold': definition.get('migration_hold', False)})
        events = []
        for event_id, run_id, payload in conn.execute(
                'SELECT id, run_id, payload FROM review_outbox ORDER BY rowid'):
            event = json.loads(payload)
            definition = next((source for source in config['sources']
                               if source['id'] == event.get('watch_id')), None)
            if (event.get('watch_id') not in match['sources'] or definition is None
                    or event.get('repository') != definition['repository']
                    or event.get('definition_hash') != _TRACKER._watch_definition_hash(
                        dict(definition, consumer='team', delivery='local'))):
                continue
            receipt = (conn.execute('SELECT receipt FROM team_receipts WHERE member=? AND event_id=?',
                                    (member, event_id)).fetchone() if has_receipts else None)
            if receipt is None:
                events.append({'id': event_id, 'run_id': run_id, 'event': event})
    return {'member': member, 'events': events, 'sources': source_rows, 'adoption_changed': False}


def acknowledge(config, member, event_id, receipt):
    """Record local review completion; do not ACK other members or adopt code."""
    if not isinstance(receipt, str) or not receipt.strip():
        raise ValueError('RECEIPT_REQUIRED')
    pending = {row['id'] for row in inbox(config, member)['events']}
    database = _database_path(config)
    if not database.is_file():
        raise ValueError('EVENT_NOT_PENDING_FOR_MEMBER')
    with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        has_receipts = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_receipts'").fetchone() is not None
        existing = (conn.execute('SELECT receipt FROM team_receipts WHERE member=? AND event_id=?',
                                 (member, event_id)).fetchone() if has_receipts else None)
        if existing is not None:
            if existing[0] != receipt:
                raise ValueError('RECEIPT_CONFLICT')
        else:
            if event_id not in pending:
                raise ValueError('EVENT_NOT_PENDING_FOR_MEMBER')
            conn.execute('CREATE TABLE IF NOT EXISTS team_receipts '
                         '(member TEXT NOT NULL, event_id TEXT NOT NULL, receipt TEXT NOT NULL, '
                         'PRIMARY KEY(member,event_id))')
            conn.execute('INSERT INTO team_receipts VALUES (?, ?, ?)', (member, event_id, receipt))
        conn.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['validate', 'tick', 'inbox', 'ack'])
    parser.add_argument('--config', required=True)
    parser.add_argument('--allow-public-network', action='store_true')
    parser.add_argument('--member')
    parser.add_argument('--event-id')
    parser.add_argument('--receipt')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.action == 'validate':
            result = {'valid': True, 'sources': len(config['sources']),
                      'members': len(config['members']), 'state_root': config['state_root']}
        elif args.action == 'tick':
            if not args.allow_public_network:
                raise ValueError('PUBLIC_NETWORK_NOT_ENABLED')
            result = collect(config)
        elif args.action == 'inbox':
            result = inbox(config, args.member)
        else:
            acknowledge(config, args.member, args.event_id, args.receipt)
            result = {'member': args.member, 'event_id': args.event_id,
                      'review_recorded': True, 'adoption_changed': False}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as exc:
        print(str(exc) if isinstance(exc, ValueError) else type(exc).__name__, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
