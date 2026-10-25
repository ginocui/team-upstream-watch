"""Public source adapters for migrated explicit git-ref duties; stdlib only."""
from __future__ import annotations
import hashlib
import http.client
import json
import os
import re
import ssl
import subprocess
import time
import xml.etree.ElementTree as ET

REPOSITORY = re.compile(r'https://github\.com/[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*')


def _repository(value):
    if not isinstance(value, str) or not REPOSITORY.fullmatch(value) or any(part in ('.', '..') for part in value.split('/')[-2:]):
        raise ValueError('REPOSITORY_URL_INVALID')
    return value


def _git_command(argv):
    env = {**os.environ, 'GIT_TERMINAL_PROMPT': '0', 'GCM_INTERACTIVE': 'never',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_OPTIONAL_LOCKS': '0',
           'GIT_ASKPASS': '', 'SSH_ASKPASS': ''}
    # Explicit fixed public HTTPS remote, no shell, installation or working-tree changes.
    proc = subprocess.run(argv, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=25)
    return proc.returncode, proc.stdout, proc.stderr


def observe_git_refs(watch, previous=None, *, command=None):
    repository = _repository(watch['repository'])
    argv = ['git', '-c', 'credential.helper=', '-c', 'core.askPass=',
            '-c', 'http.proxy=', '-c', 'http.followRedirects=false', 'ls-remote', '--', repository]
    try:
        code, stdout, stderr = (command or _git_command)(argv)
        if code != 0 or len(stdout) > 1048576:
            raise ValueError('GIT_PUBLIC_OBSERVATION_FAILED')
        refs = {}
        for line in stdout.decode('ascii').splitlines():
            sha, ref = line.split('\t')
            if not re.fullmatch('[a-f0-9]{40}', sha) or ref in refs:
                raise ValueError('GIT_REFS_INVALID')
            if ref == 'HEAD' or ref.startswith(('refs/heads/', 'refs/tags/')):
                refs[ref] = sha
        if 'HEAD' not in refs:
            raise ValueError('GIT_HEAD_MISSING')
        return {'status': 'OK', 'snapshot': {'repository': repository, 'head_sha': refs['HEAD'],
                                           'refs': dict(sorted(refs.items())), 'adoption_changed': False}, 'problems': []}
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return {'status': 'UNKNOWN', 'snapshot': None, 'problems': [{'code': 'GIT_PUBLIC_OBSERVATION_FAILED'}]}


def public_github_get(path):
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/(?:commits/[A-Za-z0-9_-]+\.atom|releases\.atom)', path):
        raise ValueError('FEED_PATH_INVALID')
    conn = http.client.HTTPSConnection('github.com', timeout=8, context=ssl.create_default_context())
    try:
        conn.request('GET', path, headers={'User-Agent': 'WE-unified-reference-watch/1', 'Accept': 'application/atom+xml'})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError('FEED_HTTP_NOT_200')
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('FEED_SIZE_LIMIT')
        return raw
    finally:
        conn.close()


def _atom_entries(raw, repository, limit):
    if len(raw) > 1048576 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('ATOM_INPUT_INVALID')
    root = ET.fromstring(raw)
    ns = '{http://www.w3.org/2005/Atom}'
    if root.tag != ns + 'feed':
        raise ValueError('ATOM_FEED_INVALID')
    rows = []
    for entry in root.findall(ns + 'entry')[:limit]:
        def text(name):
            return entry.findtext(ns + name, '')
        body = text('content') or text('summary')
        link = entry.find(ns + 'link')
        url = link.get('href', '') if link is not None else ''
        if not url.startswith(repository + '/'):
            raise ValueError('ATOM_SOURCE_MISMATCH')
        rows.append({'id': text('id'), 'title': text('title')[:300], 'updated': text('updated'), 'url': url,
                     'body_sha256': hashlib.sha256(body.encode()).hexdigest(), 'preview': body[:300],
                     'body_is_preview': len(body) > 300})
    return rows


def observe_atom(watch, previous=None, *, fetch=None):
    repository = _repository(watch['repository'])
    branch = watch.get('branch', 'main')
    if not isinstance(branch, str) or not re.fullmatch('[A-Za-z0-9_-]+', branch):
        raise ValueError('FEED_BRANCH_INVALID')
    base = '/' + '/'.join(repository.split('/')[-2:])
    get = fetch or public_github_get
    try:
        commits = _atom_entries(get(base + '/commits/' + branch + '.atom'), repository, 20)
        releases = _atom_entries(get(base + '/releases.atom'), repository, 5)
        if not commits:
            raise ValueError('ATOM_HEAD_MISSING')
        head = commits[0]['url'].rsplit('/', 1)[-1]
        if not re.fullmatch('[a-f0-9]{40}', head):
            raise ValueError('ATOM_HEAD_INVALID')
        return {'status': 'OK', 'snapshot': {'repository': repository, 'head_sha': head,
                'commits': commits, 'releases': releases, 'scope': 'main recent feed and latest five release entries only',
                'adoption_changed': False}, 'problems': []}
    except (OSError, ValueError, ET.ParseError, http.client.HTTPException):
        return {'status': 'UNKNOWN', 'snapshot': None, 'problems': [{'code': 'ATOM_PUBLIC_OBSERVATION_FAILED'}]}


def observe_public_fallback(repository, previous=None, *, command=None, fetch=None):
    """Keep public HEAD/feed observations when API coverage is unavailable."""
    repository = _repository(repository)
    result = observe_git_refs({'repository': repository}, previous, command=command)
    snapshot = {'repository': repository, 'head_sha': (result.get('snapshot') or {}).get('head_sha'),
                'releases': None, 'observation_route': 'PUBLIC_GIT_HEAD_AND_RELEASE_ATOM',
                'coverage': {'head': 'OK' if result['status'] == 'OK' else 'UNKNOWN',
                             'release_feed': 'UNKNOWN', 'api_metadata': 'UNKNOWN'},
                'scope': 'Git HEAD and at most five public release Atom entries; no API metadata or complete diff'}
    problems = list(result['problems'])
    base = '/' + '/'.join(repository.split('/')[-2:])
    try:
        snapshot['releases'] = _atom_entries((fetch or public_github_get)(base + '/releases.atom'), repository, 5)
        snapshot['coverage']['release_feed'] = 'OK'
    except (OSError, ValueError, ET.ParseError, http.client.HTTPException):
        problems.append({'code': 'PUBLIC_RELEASE_FEED_UNKNOWN', 'scope': 'release_feed'})
    known = any(snapshot['coverage'][key] == 'OK' for key in ('head', 'release_feed'))
    return {'status': 'PARTIAL' if known else 'UNKNOWN', 'snapshot': snapshot, 'problems': problems}


def _huggingface_get(path):
    conn = http.client.HTTPSConnection('huggingface.co', timeout=8, context=ssl.create_default_context())
    try:
        conn.request('GET', path, headers={'User-Agent': 'WE-unified-reference-watch/1', 'Accept': 'application/json'})
        response = conn.getresponse()
        if response.status != 200:
            raise ValueError('HF_HTTP_NOT_200')
        raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('HF_SIZE_LIMIT')
        return json.loads(raw)
    finally:
        conn.close()


def observe_huggingface(repository, previous=None, *, fetch=None):
    if not isinstance(repository, str) or not re.fullmatch(r'https://huggingface\.co/[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_-][A-Za-z0-9_.-]*', repository):
        raise ValueError('MODEL_URL_INVALID')
    model_id = '/'.join(repository.split('/')[-2:])
    try:
        data = (fetch or _huggingface_get)('/api/models/' + model_id)
        if data.get('id', '').casefold() != model_id.casefold() or not re.fullmatch('[a-f0-9]{40}', data.get('sha', '')) or data.get('private') is True:
            raise ValueError('HF_SOURCE_MISMATCH')
        license_id = (data.get('cardData') or {}).get('license')
        return {'status': 'OK', 'snapshot': {'repository': repository, 'model_id': data['id'], 'head_sha': data['sha'],
                'last_modified': data.get('lastModified'), 'gated': data.get('gated'), 'license': license_id,
                'license_not_permission_or_open_source_certification': True, 'weights_downloaded': False}, 'problems': []}
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return {'status': 'UNKNOWN', 'snapshot': None, 'problems': [{'code': 'HF_PUBLIC_OBSERVATION_FAILED'}]}
