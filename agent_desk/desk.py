#!/usr/bin/env python3
"""Desk's local, cooperative coordination ledger. No provider or browser automation.

Run `desk.py --help` for commands and `desk.py COMMAND --help` for payloads.
The SQLite file is authoritative; UI snapshots are disposable read-only projections.
"""
import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
import uuid

# Callers also load desk.py by exact path with importlib; resolve its sibling
# without changing sys.path or accidentally loading another installed checkout.
_request_spec = importlib.util.spec_from_file_location('desk_request_mixin', Path(__file__).resolve().with_name('desk_requests.py'))
_request_module = importlib.util.module_from_spec(_request_spec)
_request_spec.loader.exec_module(_request_module)
RequestMixin, REQUEST_CONTRACT, REQUEST_SCHEMA = (_request_module.RequestMixin, _request_module.REQUEST_CONTRACT, _request_module.REQUEST_SCHEMA)
_project_spec = importlib.util.spec_from_file_location('desk_project_mixin', Path(__file__).resolve().with_name('desk_projects.py'))
_project_module = importlib.util.module_from_spec(_project_spec)
_project_spec.loader.exec_module(_project_module)
ProjectMixin, PROJECT_CONTRACT, PROJECT_SCHEMA = (_project_module.ProjectMixin, _project_module.PROJECT_CONTRACT, _project_module.PROJECT_SCHEMA)
_lifecycle_spec = importlib.util.spec_from_file_location('desk_lifecycle_mixin', Path(__file__).resolve().with_name('desk_lifecycle.py'))
_lifecycle_module = importlib.util.module_from_spec(_lifecycle_spec)
_lifecycle_spec.loader.exec_module(_lifecycle_module)
LifecycleMixin, LIFECYCLE_CONTRACT, TERMINAL_PR_STATES = (_lifecycle_module.LifecycleMixin, _lifecycle_module.LIFECYCLE_CONTRACT, _lifecycle_module.TERMINAL_PR_STATES)

STATE_DIR = Path(os.environ.get('AGENT_DESK_HOME', str(Path.home() / '.local/state/agent-desk')))
DEFAULT_DB = Path(os.environ.get('DESK_DB', str(STATE_DIR / 'coordination.sqlite3')))

CONTRACT = {
    'lane-register': 'provider, session_id, host_id; optional label, role (agent|cos), lane_id (stable deterministic ID is returned)',
    'workstream-bind': 'workstream_key (lowercase hyphenated key, 1..200 characters), lane_id (registered nonhuman lane), expected_version (0 creates; exact current version required for existing bindings); optional reason. Current-version same assignment is idempotent',
    'consumer-register': 'lane_id, mechanism (manual|native_wakeup|native_inbox|native_poll), metadata (object); optional ttl_seconds (30..3600), consumer_id (renew current ID)',
    'consumer-heartbeat': 'lane_id, consumer_id; optional ttl_seconds (30..3600)',
    'pr-own': 'repo, pr, owner_lane; optional url, workstream_key (lowercase hyphenated key, 1..200 characters), title (1..500 characters), opened_by_lane (registered lane; defaults to owner for new PRs, immutable once known). observed_status ({status: active|completed|abandoned, auto_complete: boolean, checked_at: ISO UTC timestamp}). Changed metadata increments version',
    'pr-transfer': 'repo, pr, expected_owner, expected_version, new_owner; optional reason',
    'pr-merge': 'repo, pr, owner_lane, expected_version, merge_commit',
    'pr-promote': 'repo, pr, cos_lane, expected_version, attestation {release, deployed_commit, included_commits:[...], runtime_evidence:[nonempty strings]}; optional notify_owner (boolean, default false) atomically queues one factual update to recorded owner and returns notification_message_id',
    'subscribe': 'lane_id, topic (nonempty stable topic, e.g. pr:repo:123); optional active (boolean)',
    'message-reply': 'message_id, lane_id, body, dedup_key; optional kind, ttl_seconds; agent requires consumer_id, claim_id and acceptance; human identity must be supplied by trusted caller',
    'message-done': 'message_id, lane_id; optional resolution; agent requires consumer_id, claim_id and acceptance',
    'message-history': 'optional conversation_id (omit for all), limit (1..100), before_id (exclusive cursor); returns newest first with next_before_id',
    'message-send': 'from_lane, to_lane, kind (update|question|work_request), dedup_key, body; optional workstream_key (lowercase hyphenated key, 1..200 characters), ttl_seconds (1..604800), parent_id (agent reply requires consumer_id and claim_id). Project context is captured from the registered card on conversation creation and inherited unchanged by replies; caller-supplied context is rejected',
    'broadcast-send': 'from_lane (registered human), dedup_key, body (1..4000 characters), expected_recipients (exact connected native agent lane IDs). Audience changes fail for refresh; exact retries keep the original audience',
    'inbox-claim': 'lane_id, consumer_id; optional limit (1..20), lease_seconds (10..300)',
    'inbox-renew': 'lane_id, consumer_id, claim_id; optional lease_seconds (10..300); extends current lease and consumer heartbeat',
    'inbox-release': 'lane_id, consumer_id, claim_id (release an active delivery batch)',
    'message-accept': 'message_id, lane_id, consumer_id, claim_id',
    'message-resolve': 'message_id, lane_id, consumer_id, claim_id; optional resolution (requires accepted message and current lease)',
    'incident-report': 'repo, build, reporter_lane, owner_lane, evidence (nonempty string); optional ttl_seconds (1..604800)',
    'incident-resolve': 'incident_id, owner_lane, resolution',
    'pause': 'paused (boolean)',
    'snapshot': 'optional limit (1..1000, default 200), lane_id (registered lane scope: own/subscribed PRs and incidents, sent/received messages, own subscriptions, workstream bindings and requests); optional request_history_limit (0..1000, default 100). All nonterminal requests are included independently of limit. Returns counts and truncated per table; read-only, never creates a database',
}

CONTRACT.update(REQUEST_CONTRACT)
CONTRACT.update(PROJECT_CONTRACT)
CONTRACT.update(LIFECYCLE_CONTRACT)
CONTRACT['snapshot'] += '; project/arc/card/promotion receipt arrays are additive, with optional arc_history_limit (0..1000) and arc_history_since (UTC epoch seconds)'

SCHEMA = '''
CREATE TABLE IF NOT EXISTS lanes (
 lane_id TEXT PRIMARY KEY, provider TEXT NOT NULL, session_id TEXT NOT NULL, host_id TEXT NOT NULL,
 label TEXT NOT NULL, role TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(provider,session_id,host_id));
CREATE TABLE IF NOT EXISTS workstreams (
 workstream_key TEXT PRIMARY KEY, lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
 version INTEGER NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS consumers (
 lane_id TEXT PRIMARY KEY REFERENCES lanes(lane_id), consumer_id TEXT NOT NULL,
 mechanism TEXT NOT NULL, metadata TEXT NOT NULL, heartbeat_at REAL NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS prs (
 repo TEXT NOT NULL, pr TEXT NOT NULL, owner_lane TEXT NOT NULL REFERENCES lanes(lane_id),
 version INTEGER NOT NULL, status TEXT NOT NULL, url TEXT, merge_commit TEXT, attestation TEXT,
 updated_at REAL NOT NULL, workstream_key TEXT, title TEXT, opened_by_lane TEXT REFERENCES lanes(lane_id), observed_status TEXT, closure TEXT,
 PRIMARY KEY(repo,pr));
CREATE TABLE IF NOT EXISTS subscriptions (
 lane_id TEXT NOT NULL REFERENCES lanes(lane_id), topic TEXT NOT NULL, active INTEGER NOT NULL,
 created_at REAL NOT NULL, PRIMARY KEY(lane_id,topic));
CREATE TABLE IF NOT EXISTS messages (
 message_id TEXT PRIMARY KEY, from_lane TEXT NOT NULL REFERENCES lanes(lane_id),
 to_lane TEXT NOT NULL REFERENCES lanes(lane_id), kind TEXT NOT NULL, dedup_key TEXT NOT NULL,
 body TEXT NOT NULL, parent_id TEXT REFERENCES messages(message_id), round INTEGER NOT NULL,
 state TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 delivered_at REAL, accepted_at REAL, resolved_at REAL, resolution TEXT, claim_id TEXT, workstream_key TEXT, context TEXT,
 UNIQUE(from_lane,dedup_key));
CREATE TABLE IF NOT EXISTS broadcasts (
 broadcast_id TEXT PRIMARY KEY, from_lane TEXT NOT NULL REFERENCES lanes(lane_id),
 dedup_key TEXT NOT NULL, body TEXT NOT NULL, recipients TEXT NOT NULL, created_at REAL NOT NULL,
 UNIQUE(from_lane,dedup_key));
CREATE TABLE IF NOT EXISTS claims (
 claim_id TEXT PRIMARY KEY, lane_id TEXT NOT NULL REFERENCES lanes(lane_id), consumer_id TEXT NOT NULL,
 created_at REAL NOT NULL, expires_at REAL NOT NULL, released_at REAL);
CREATE INDEX IF NOT EXISTS ix_claims_lane ON claims(lane_id,created_at);
CREATE INDEX IF NOT EXISTS ix_messages_parent ON messages(parent_id);
CREATE INDEX IF NOT EXISTS ix_messages_to ON messages(to_lane,state,expires_at);
CREATE TABLE IF NOT EXISTS incidents (
 incident_id TEXT PRIMARY KEY, repo TEXT NOT NULL, build TEXT NOT NULL,
 owner_lane TEXT NOT NULL REFERENCES lanes(lane_id), state TEXT NOT NULL,
 created_at REAL NOT NULL, resolved_at REAL, resolution TEXT, UNIQUE(repo,build));
CREATE TABLE IF NOT EXISTS incident_evidence (
 incident_id TEXT NOT NULL REFERENCES incidents(incident_id), evidence_hash TEXT NOT NULL,
 evidence TEXT NOT NULL, reporter_lane TEXT NOT NULL REFERENCES lanes(lane_id), created_at REAL NOT NULL,
 PRIMARY KEY(incident_id,evidence_hash));
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, action TEXT NOT NULL, detail TEXT NOT NULL, created_at REAL NOT NULL);
'''

SCHEMA += REQUEST_SCHEMA
SCHEMA += PROJECT_SCHEMA

class DeskError(ValueError):
    pass


def required(data, *keys):
    for key in keys:
        if key not in data or data[key] is None or isinstance(data[key], (dict, list, bool)) or not str(data[key]).strip():
            raise DeskError(f'{key} is required and must be a nonempty scalar')


def bounded(data, key, default, low, high):
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise DeskError(f'{key} must be an integer in {low}..{high}')
    return value


def uid():
    return str(uuid.uuid4())


class Desk(RequestMixin, ProjectMixin, LifecycleMixin):
    """Transactions serialize ownership, deduplication and inbox leases across processes."""
    request_error_type = DeskError
    project_error_type = DeskError
    lifecycle_error_type = DeskError

    def __init__(self, path=None, clock=time.time):
        self.path = Path(DEFAULT_DB if path is None else path).expanduser()
        self.clock = clock

    @contextlib.contextmanager
    def connection(self, readonly=False):
        if readonly:
            if not self.path.exists():
                yield None
                return
            con = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=10)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            con = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            os.chmod(self.path, 0o600)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            if not readonly:
                con.executescript(SCHEMA)
                con.execute('BEGIN IMMEDIATE')
                # Serialize this additive migration with writers; read-only calls never migrate.
                message_columns = {r['name'] for r in con.execute('PRAGMA table_info(messages)')}
                if 'workstream_key' not in message_columns:
                    con.execute('ALTER TABLE messages ADD COLUMN workstream_key TEXT')
                if 'context' not in message_columns:
                    con.execute('ALTER TABLE messages ADD COLUMN context TEXT')
                if 'reported_at' not in {r['name'] for r in con.execute('PRAGMA table_info(cards)')}:
                    con.execute('ALTER TABLE cards ADD COLUMN reported_at REAL')
                pr_columns = {r['name'] for r in con.execute('PRAGMA table_info(prs)')}
                for name, definition in (('workstream_key', 'TEXT'), ('title', 'TEXT'), ('opened_by_lane', 'TEXT REFERENCES lanes(lane_id)'), ('observed_status', 'TEXT'), ('closure', 'TEXT')):
                    if name not in pr_columns:
                        con.execute(f'ALTER TABLE prs ADD COLUMN {name} {definition}')
                request_columns = {r['name'] for r in con.execute('PRAGMA table_info(requests)')}
                for name, definition in (('cancellation', 'TEXT'), ('owner_managed', 'INTEGER NOT NULL DEFAULT 0')):
                    if name not in request_columns:
                        con.execute(f'ALTER TABLE requests ADD COLUMN {name} {definition}')
            else:
                con.execute('BEGIN')
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def execute(self, command, data=None):
        data = {} if data is None else data
        if not isinstance(data, dict):
            raise DeskError('payload must be a JSON object')
        if command not in CONTRACT:
            raise DeskError('unknown command')
        with self.connection(readonly=command in ('snapshot', 'message-history', 'request-list')) as con:
            changes_before = con.total_changes if con is not None else 0
            result = getattr(self, command.replace('-', '_'))(con, data, self.clock())
            if (con is not None and command not in ('snapshot', 'message-history', 'request-list', 'request-reconcile')
                    and (not (command.startswith('request-') or command in PROJECT_CONTRACT) or con.total_changes > changes_before)):
                con.execute('INSERT INTO audit(action,detail,created_at) VALUES(?,?,?)',
                            (command, json.dumps(data, sort_keys=True), self.clock()))
            return result

    def lane(self, con, lane_id):
        row = con.execute('SELECT * FROM lanes WHERE lane_id=?', (lane_id,)).fetchone()
        if not row:
            raise DeskError('lane is not registered: ' + str(lane_id))
        return dict(row)

    def consumer(self, con, data, now):
        required(data, 'lane_id', 'consumer_id')
        row = con.execute('SELECT * FROM consumers WHERE lane_id=?', (data['lane_id'],)).fetchone()
        if not row or row['consumer_id'] != data['consumer_id'] or row['expires_at'] <= now:
            raise DeskError('consumer is missing, expired, or replaced')
        return dict(row)

    def lane_register(self, con, data, now):
        required(data, 'provider', 'session_id', 'host_id')
        identity = tuple(str(data[k]).strip() for k in ('provider', 'session_id', 'host_id'))
        lane_id = str(uuid.uuid5(uuid.NAMESPACE_URL, json.dumps(identity)))
        if data.get('lane_id', lane_id) != lane_id:
            raise DeskError('lane_id does not match the stable provider/session/host identity')
        role = data.get('role', 'agent')
        if role not in ('agent', 'cos'):
            raise DeskError('role must be agent or cos')
        row = con.execute('SELECT * FROM lanes WHERE lane_id=?', (lane_id,)).fetchone()
        if row:
            if 'role' in data and row['role'] != role:
                raise DeskError('registered lane role is immutable')
            if 'label' in data:
                label = str(data['label']).strip()
                if not label:
                    raise DeskError('lane label must not be empty')
                con.execute('UPDATE lanes SET label=? WHERE lane_id=?', (label, lane_id))
                return self.lane(con, lane_id)
            return dict(row)
        con.execute('INSERT INTO lanes VALUES(?,?,?,?,?,?,?)',
                    (lane_id, *identity, str(data.get('label', identity[1])), role, now))
        return self.lane(con, lane_id)

    def workstream_bind(self, con, data, now):
        required(data, 'workstream_key', 'lane_id', 'expected_version')
        key = data['workstream_key']
        if not isinstance(key, str) or len(key) > 200 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', key):
            raise DeskError('workstream_key must be a lowercase hyphenated key of 1..200 characters')
        expected = bounded(data, 'expected_version', 0, 0, 9223372036854775807)
        if not isinstance(data['lane_id'], str):
            raise DeskError('lane_id must be a registered nonhuman lane')
        lane = self.lane(con, data['lane_id'])
        if lane['provider'].casefold() == 'human':
            raise DeskError('workstream binding requires a nonhuman lane')
        if 'reason' in data and (not isinstance(data['reason'], str) or not data['reason'].strip()):
            raise DeskError('reason must be a nonempty string')
        row = con.execute('SELECT * FROM workstreams WHERE workstream_key=?', (key,)).fetchone()
        if expected != (row['version'] if row else 0):
            raise DeskError('stale workstream version')
        if row and row['lane_id'] == lane['lane_id']:
            return dict(row)
        if row:
            con.execute('UPDATE workstreams SET lane_id=?,version=version+1,updated_at=? WHERE workstream_key=?',
                        (lane['lane_id'], now, key))
        else:
            con.execute('INSERT INTO workstreams VALUES(?,?,1,?)', (key, lane['lane_id'], now))
        return dict(con.execute('SELECT * FROM workstreams WHERE workstream_key=?', (key,)).fetchone())

    def consumer_register(self, con, data, now):
        required(data, 'lane_id', 'mechanism')
        self.lane(con, data['lane_id'])
        if data['mechanism'] not in ('manual', 'native_wakeup', 'native_inbox', 'native_poll'):
            raise DeskError('unsupported consumer mechanism')
        metadata = data.get('metadata', {})
        if not isinstance(metadata, dict):
            raise DeskError('metadata must be an object')
        ttl = bounded(data, 'ttl_seconds', 300, 30, 3600)
        old = con.execute('SELECT * FROM consumers WHERE lane_id=?', (data['lane_id'],)).fetchone()
        if old and old['expires_at'] > now and old['consumer_id'] != data.get('consumer_id'):
            raise DeskError('lane already has a live consumer')
        consumer_id = old['consumer_id'] if old and old['consumer_id'] == data.get('consumer_id') else uid()
        con.execute('INSERT OR REPLACE INTO consumers VALUES(?,?,?,?,?,?)',
                    (data['lane_id'], consumer_id, data['mechanism'], json.dumps(metadata), now, now + ttl))
        return dict(con.execute('SELECT * FROM consumers WHERE lane_id=?', (data['lane_id'],)).fetchone())

    def consumer_heartbeat(self, con, data, now):
        self.consumer(con, data, now)
        ttl = bounded(data, 'ttl_seconds', 300, 30, 3600)
        con.execute('UPDATE consumers SET heartbeat_at=?,expires_at=? WHERE lane_id=?', (now, now + ttl, data['lane_id']))
        return {'lane_id': data['lane_id'], 'consumer_id': data['consumer_id'], 'expires_at': now + ttl}

    def get_pr(self, con, data):
        data = self.canonical_pr_payload(data)
        required(data, 'repo', 'pr')
        row = con.execute('SELECT * FROM prs WHERE repo=? AND pr=?', (data['repo'], str(data['pr']))).fetchone()
        if not row:
            raise DeskError('PR is not owned')
        return dict(row)

    def pr_own(self, con, data, now):
        data = self.canonical_pr_payload(data)
        required(data, 'repo', 'pr', 'owner_lane')
        self.lane(con, data['owner_lane'])
        workstream_key = data.get('workstream_key')
        if workstream_key is not None and (not isinstance(workstream_key, str) or len(workstream_key) > 200 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', workstream_key)):
            raise DeskError('workstream_key must be a lowercase hyphenated key of 1..200 characters')
        title = data.get('title')
        if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 500):
            raise DeskError('title must be a nonempty string of 1..500 characters')
        if 'opened_by_lane' in data:
            if not isinstance(data['opened_by_lane'], str) or not data['opened_by_lane'].strip():
                raise DeskError('opened_by_lane must be a registered lane')
            self.lane(con, data['opened_by_lane'])
        if 'observed_status' in data:
            observed = data['observed_status']
            if not isinstance(observed, dict) or set(observed) != {'status', 'auto_complete', 'checked_at'}:
                raise DeskError('observed_status requires status, auto_complete and checked_at')
            if observed['status'] not in ('active', 'completed', 'abandoned') or not isinstance(observed['auto_complete'], bool):
                raise DeskError('observed_status requires a valid status and boolean auto_complete')
            try:
                stamp = observed['checked_at']
                if not isinstance(stamp, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)', stamp):
                    raise ValueError()
                parsed = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
                    raise ValueError()
            except (ValueError, TypeError):
                raise DeskError('observed_status.checked_at must be an ISO UTC timestamp')
            data = dict(data, observed_status=json.dumps(observed, sort_keys=True))
        existing = con.execute('SELECT * FROM prs WHERE repo=? AND pr=?', (data['repo'], str(data['pr']))).fetchone()
        if existing:
            if existing['owner_lane'] != data['owner_lane']:
                raise DeskError('PR already has an owner; explicit CAS transfer required')
            if 'opened_by_lane' in data and existing['opened_by_lane'] is not None and existing['opened_by_lane'] != data['opened_by_lane']:
                raise DeskError('opened_by_lane is immutable once known')
            metadata = {key: data[key] for key in ('url', 'workstream_key', 'title', 'opened_by_lane', 'observed_status') if key in data and data[key] != existing[key]}
            if metadata:
                assignments = ','.join(key + '=?' for key in metadata)
                con.execute(f'UPDATE prs SET {assignments},version=version+1,updated_at=? WHERE repo=? AND pr=?',
                            (*metadata.values(), now, data['repo'], str(data['pr'])))
            if existing['status'] not in TERMINAL_PR_STATES:
                self.subscribe(con, {'lane_id': data['owner_lane'], 'topic': 'pr:' + data['repo'] + ':' + str(data['pr'])}, now)
            return self.get_pr(con, data)
        con.execute('INSERT INTO prs(repo,pr,owner_lane,version,status,url,merge_commit,attestation,updated_at,workstream_key,title,opened_by_lane,observed_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (data['repo'], str(data['pr']), data['owner_lane'], 1, 'owned', data.get('url'), None, None, now,
                     workstream_key, title, data.get('opened_by_lane', data['owner_lane']), data.get('observed_status')))
        self.subscribe(con, {'lane_id': data['owner_lane'], 'topic': 'pr:' + data['repo'] + ':' + str(data['pr'])}, now)
        return self.get_pr(con, data)

    def pr_cas(self, row, data, owner_key):
        required(data, owner_key, 'expected_version')
        if data[owner_key] != row['owner_lane'] or data['expected_version'] != row['version']:
            raise DeskError('stale PR owner or version')
        if row['status'] in TERMINAL_PR_STATES:
            raise DeskError(row['status'] + ' PR is closed')

    def pr_transfer(self, con, data, now):
        data = self.canonical_pr_payload(data)
        row = self.get_pr(con, data)
        self.pr_cas(row, data, 'expected_owner')
        required(data, 'new_owner')
        self.lane(con, data['new_owner'])
        con.execute('UPDATE prs SET owner_lane=?,version=version+1,updated_at=? WHERE repo=? AND pr=?',
                    (data['new_owner'], now, row['repo'], row['pr']))
        self.subscribe(con, {'lane_id': data['new_owner'], 'topic': 'pr:' + row['repo'] + ':' + row['pr']}, now)
        return self.get_pr(con, data)

    def pr_merge(self, con, data, now):
        data = self.canonical_pr_payload(data)
        row = self.get_pr(con, data)
        self.pr_cas(row, data, 'owner_lane')
        required(data, 'merge_commit')
        if row['status'] != 'owned':
            raise DeskError('PR must be owned before merge is recorded')
        con.execute('UPDATE prs SET status=?,merge_commit=?,version=version+1,updated_at=? WHERE repo=? AND pr=?',
                    ('merged', data['merge_commit'], now, row['repo'], row['pr']))
        result = self.get_pr(con, data)
        self._lifecycle_reconcile_requests(con, now)
        return result

    def pr_promote(self, con, data, now):
        data = self.canonical_pr_payload(data)
        notify_owner = data.get('notify_owner', False)
        if not isinstance(notify_owner, bool):
            raise DeskError('notify_owner must be boolean')
        row = self.get_pr(con, data)
        required(data, 'cos_lane', 'expected_version')
        if self.lane(con, data['cos_lane'])['role'] != 'cos':
            raise DeskError('promotion requires a registered CoS lane')
        if row['status'] != 'merged' or row['version'] != data['expected_version']:
            raise DeskError('promotion requires current merged PR version')
        att = data.get('attestation')
        if not isinstance(att, dict):
            raise DeskError('attestation must be an object')
        required(att, 'release', 'deployed_commit')
        commits, evidence = att.get('included_commits'), att.get('runtime_evidence')
        if not isinstance(commits, list) or row['merge_commit'] not in commits or att['deployed_commit'] not in commits:
            raise DeskError('attestation must include both merge and deployed commits in included_commits')
        if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x.strip() for x in evidence):
            raise DeskError('runtime_evidence must contain nonempty evidence references')
        att = dict(att, cos_lane=data['cos_lane'], attested_at=now)
        con.execute('UPDATE prs SET status=?,attestation=?,version=version+1,updated_at=? WHERE repo=? AND pr=?',
                    ('promoted', json.dumps(att), now, row['repo'], row['pr']))
        con.execute('UPDATE subscriptions SET active=0 WHERE topic=?', ('pr:' + row['repo'] + ':' + row['pr'],))
        result = self.get_pr(con, data)
        if notify_owner:
            body = f"Promotion recorded for PR #{row['pr']}. Release: {str(att['release'])[:200]}"
            if row.get('url'):
                body += '\nPR: ' + str(row['url'])[:2000]
            body += '\nEvidence:\n' + '\n'.join('- ' + item[:500] for item in evidence[:3])
            identity = json.dumps([row['repo'], row['pr'], result['version'], row['merge_commit']])
            notice = self.message_send(con, {
                'from_lane': data['cos_lane'], 'to_lane': row['owner_lane'], 'kind': 'update',
                'dedup_key': 'pr-promoted:' + hashlib.sha256(identity.encode()).hexdigest(),
                'body': body, 'workstream_key': row.get('workstream_key'),
            }, now)
            result['notification_message_id'] = notice['message_id']
        self._lifecycle_reconcile_requests(con, now)
        return result

    def subscribe(self, con, data, now):
        required(data, 'lane_id', 'topic')
        self.lane(con, data['lane_id'])
        active = data.get('active', True)
        if not isinstance(active, bool):
            raise DeskError('active must be boolean')
        pr = con.execute("SELECT status FROM prs WHERE 'pr:' || repo || ':' || pr=?", (data['topic'],)).fetchone()
        if data['topic'].startswith('pr:') and not active and (not pr or pr['status'] not in TERMINAL_PR_STATES):
            raise DeskError('PR subscriptions close only after CoS promotion')
        if pr and pr['status'] in TERMINAL_PR_STATES and active:
            raise DeskError(pr['status'] + ' PR subscriptions are closed')
        con.execute('INSERT INTO subscriptions VALUES(?,?,?,?) ON CONFLICT(lane_id,topic) DO UPDATE SET active=excluded.active',
                    (data['lane_id'], data['topic'], int(active), now))
        return {'lane_id': data['lane_id'], 'topic': data['topic'], 'active': active}

    def broadcast_send(self, con, data, now):
        if set(data) != {'from_lane', 'dedup_key', 'body', 'expected_recipients'}:
            raise DeskError('broadcast requires from_lane, dedup_key, body and expected_recipients')
        required(data, 'from_lane', 'dedup_key', 'body')
        expected = data['expected_recipients']
        if not isinstance(expected, list) or not 1 <= len(expected) <= 100 or any(not isinstance(item, str) or not re.fullmatch(r'[0-9a-f-]{36}', item) for item in expected) or len(set(expected)) != len(expected):
            raise DeskError('expected_recipients must be unique lane IDs')
        sender = self.lane(con, data['from_lane'])
        if sender['provider'] != 'human':
            raise DeskError('broadcast sender must be a registered human')
        body = data['body']
        if not isinstance(body, str) or not body.strip() or len(body) > 4000 or '\0' in body:
            raise DeskError('broadcast body must contain 1..4000 characters without NUL')
        key = data['dedup_key']
        if not isinstance(key, str) or len(key) > 200 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]*', key):
            raise DeskError('invalid broadcast dedup_key')
        prior = con.execute('SELECT * FROM broadcasts WHERE from_lane=? AND dedup_key=?',
                            (sender['lane_id'], key)).fetchone()
        if prior:
            if prior['body'] != body or sorted(r['lane_id'] for r in json.loads(prior['recipients'])) != sorted(expected):
                raise DeskError('broadcast dedup_key already used with different content')
            return {'broadcast_id': prior['broadcast_id'], 'recipients': json.loads(prior['recipients']),
                    'recipient_count': len(json.loads(prior['recipients'])), 'duplicate': True}
        recipients = []
        for lane in con.execute("SELECT l.lane_id,l.label,c.metadata FROM lanes l JOIN consumers c ON c.lane_id=l.lane_id "
                                "WHERE l.provider!='human' AND c.expires_at>? AND c.mechanism IN "
                                "('native_wakeup','native_inbox','native_poll') ORDER BY l.lane_id", (now,)):
            metadata = json.loads(lane['metadata'])
            if metadata.get('retired') is True or metadata.get('paused') is True or metadata.get('enabled') is False:
                continue
            expiry = metadata.get('expires_at')
            if expiry:
                try:
                    if datetime.fromisoformat(expiry.replace('Z', '+00:00')).timestamp() <= now:
                        continue
                except (AttributeError, TypeError, ValueError):
                    continue
            recipients.append({'lane_id': lane['lane_id'], 'label': lane['label']})
        if sorted(r['lane_id'] for r in recipients) != sorted(expected):
            raise DeskError('Active agents changed; refresh the recipient list and try again')
        broadcast_id = uid()
        con.execute('INSERT INTO broadcasts VALUES(?,?,?,?,?,?)',
                    (broadcast_id, sender['lane_id'], key, body, json.dumps(recipients), now))
        for recipient in recipients:
            self.message_send(con, {'from_lane': sender['lane_id'], 'to_lane': recipient['lane_id'],
                                    'kind': 'update', 'dedup_key': 'broadcast:' + broadcast_id + ':' + recipient['lane_id'],
                                    'body': body, 'ttl_seconds': 604800}, now)
        return {'broadcast_id': broadcast_id, 'recipients': recipients,
                'recipient_count': len(recipients), 'duplicate': False}

    def message_send(self, con, data, now):
        required(data, 'from_lane', 'to_lane', 'kind', 'dedup_key', 'body')
        if 'context' in data:
            raise DeskError('message context is derived from the ledger and cannot be supplied by callers')
        if data['kind'] not in ('update', 'question', 'work_request'):
            raise DeskError('unsupported message kind')
        self.lane(con, data['from_lane'])
        self.lane(con, data['to_lane'])
        ttl = bounded(data, 'ttl_seconds', 86400, 1, 604800)
        workstream_key = data.get('workstream_key')
        if workstream_key is not None and (not isinstance(workstream_key, str) or len(workstream_key) > 200 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', workstream_key)):
            raise DeskError('workstream_key must be a lowercase hyphenated key of 1..200 characters')
        parent = None
        context = None
        if data.get('parent_id'):
            parent = con.execute('SELECT * FROM messages WHERE message_id=?', (data['parent_id'],)).fetchone()
            if not parent or parent['to_lane'] != data['from_lane'] or parent['from_lane'] != data['to_lane']:
                raise DeskError('reply parent must be a message from the recipient to the sender')
            projected_parent = self.conversation_projection(con, now)[parent['message_id']]
            inherited = projected_parent['workstream_key']
            if 'workstream_key' in data and workstream_key != inherited:
                raise DeskError('reply cannot change conversation workstream_key')
            workstream_key = inherited
            context = projected_parent['context']
        prior = con.execute('SELECT * FROM messages WHERE from_lane=? AND dedup_key=?',
                            (data['from_lane'], data['dedup_key'])).fetchone()
        if prior:
            if prior['workstream_key'] != workstream_key or any(prior[k] != data.get(k) for k in ('to_lane', 'kind', 'body', 'parent_id')):
                raise DeskError('dedup_key already used with different message content')
            return dict(self.message_record(prior), duplicate=True)
        round_number = 0
        if parent is not None:
            self.reply_authority(con, parent, dict(data, lane_id=data['from_lane']), now)
            round_number = 0 if self.lane(con, data['from_lane'])['provider'] == 'human' else parent['round'] + 1
        else:
            context = self.card_message_context(con, workstream_key, now)
        message_id = uid()
        con.execute('INSERT INTO messages(message_id,from_lane,to_lane,kind,dedup_key,body,parent_id,round,state,created_at,expires_at,workstream_key,context) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (message_id, data['from_lane'], data['to_lane'], data['kind'], data['dedup_key'], data['body'], data.get('parent_id'), round_number, 'queued', now, now + ttl, workstream_key,
                     json.dumps(context, sort_keys=True) if context is not None else None))
        if parent is not None:
            con.execute("UPDATE messages SET state='resolved',resolved_at=?,resolution=? WHERE message_id=?",
                        (now, 'Replied with message ' + message_id, parent['message_id']))
        return dict(self.message_record(con.execute('SELECT * FROM messages WHERE message_id=?', (message_id,)).fetchone()), duplicate=False)

    def message_record(self, row):
        result = dict(row)
        result['context'] = json.loads(result['context']) if result.get('context') else None
        return result

    def reply_authority(self, con, row, data, now, allow_done=False):
        required(data, 'lane_id')
        actor = self.lane(con, data['lane_id'])
        if not row or row['to_lane'] != data['lane_id']:
            raise DeskError('message is absent or belongs to another recipient')
        if con.execute('SELECT 1 FROM messages WHERE parent_id=?', (row['message_id'],)).fetchone():
            raise DeskError('message already has a reply; refresh the conversation')
        if row['state'] == 'resolved' and not allow_done:
            raise DeskError('message is already done; refresh the conversation')
        if actor['provider'] != 'human' and row['expires_at'] <= now and row['state'] not in ('accepted', 'resolved'):
            raise DeskError('message has expired')
        if actor['provider'] != 'human':
            self.active_claim(con, data, now)
            if row['claim_id'] != data['claim_id'] or row['state'] not in (('accepted', 'resolved') if allow_done else ('accepted',)):
                raise DeskError('agent reply or done requires accepted message in its active claim')

    def message_reply(self, con, data, now):
        required(data, 'message_id', 'lane_id', 'body', 'dedup_key')
        row = con.execute('SELECT * FROM messages WHERE message_id=?', (data['message_id'],)).fetchone()
        if not row or row['to_lane'] != data['lane_id']:
            raise DeskError('message is absent or belongs to another recipient')
        return self.message_send(con, dict(data, from_lane=data['lane_id'], to_lane=row['from_lane'],
                                          parent_id=row['message_id'], kind=data.get('kind', 'update')), now)

    def message_done(self, con, data, now):
        required(data, 'message_id', 'lane_id')
        row = con.execute('SELECT * FROM messages WHERE message_id=?', (data['message_id'],)).fetchone()
        self.reply_authority(con, row, data, now, allow_done=True)
        if row['state'] != 'resolved':
            con.execute("UPDATE messages SET state='resolved',resolved_at=?,resolution=? WHERE message_id=?",
                        (now, data.get('resolution', 'Done'), row['message_id']))
        return self.message_record(con.execute('SELECT * FROM messages WHERE message_id=?', (row['message_id'],)).fetchone())

    def conversation_projection(self, con, now):
        # Use the entire ledger: page boundaries must never resurrect answered messages.
        rows = [dict(r) for r in con.execute('SELECT rowid AS sequence,* FROM messages ORDER BY rowid')]
        humans = {r[0] for r in con.execute("SELECT lane_id FROM lanes WHERE provider='human'")}
        by_id = {r['message_id']: r for r in rows}
        roots = {}
        for row in rows:
            path, current = [], row['message_id']
            while current not in roots:
                path.append(current)
                parent = by_id[current]['parent_id']
                if not parent or parent not in by_id:
                    roots[current] = current
                    break
                current = parent
            for item in path:
                roots[item] = roots[current]
        latest = {}
        for row in rows:
            row['conversation_id'] = roots[row['message_id']]
            row['workstream_key'] = by_id[row['conversation_id']].get('workstream_key')
            raw_context = by_id[row['conversation_id']].get('context')
            row['context'] = json.loads(raw_context) if isinstance(raw_context, str) else raw_context
            latest[row['conversation_id']] = row
        for row in rows:
            tip = latest[row['conversation_id']]
            row['current'] = row['message_id'] == tip['message_id']
            row['conversation_done'] = tip['state'] == 'resolved'
            row['expired'] = row['to_lane'] not in humans and row['expires_at'] <= now and row['state'] not in ('accepted', 'resolved')
            row['pending'] = row['current'] and row['state'] != 'resolved' and not row['expired']
        return by_id

    def message_history(self, con, data, now):
        limit = bounded(data, 'limit', 50, 1, 100)
        conversation_id = data.get('conversation_id')
        projected = self.conversation_projection(con, now) if con is not None else {}
        rows = [r for r in projected.values() if conversation_id is None or r['conversation_id'] == conversation_id]
        count = len(rows)
        if not rows and conversation_id is not None:
            raise DeskError('conversation is absent')
        rows.sort(key=lambda r: r['sequence'], reverse=True)
        if data.get('before_id'):
            cursor = next((r for r in rows if r['message_id'] == data['before_id']), None)
            if cursor is None:
                raise DeskError('history cursor does not belong to conversation')
            rows = [r for r in rows if r['sequence'] < cursor['sequence']]
        page = rows[:limit]
        return {'conversation_id': conversation_id, 'messages': page, 'count': count,
                'has_more': len(rows) > limit,
                'next_before_id': page[-1]['message_id'] if len(rows) > limit else None}

    def is_paused(self, con):
        row = con.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        return bool(row and row['value'] == 'true')

    def inbox_claim(self, con, data, now):
        self.consumer(con, data, now)
        limit = bounded(data, 'limit', 3, 1, 20)
        lease = bounded(data, 'lease_seconds', 120, 10, 300)
        if self.is_paused(con):
            return {'claimed': False, 'reason': 'paused', 'messages': []}
        active = con.execute('SELECT * FROM claims WHERE lane_id=? AND expires_at>? AND released_at IS NULL', (data['lane_id'], now)).fetchone()
        if active:
            return {'claimed': False, 'reason': 'active_lease', 'messages': []}
        # Accepted but unfinished work is reclaimed after a consumer crash, retaining its acceptance.
        rows = con.execute("SELECT * FROM messages WHERE to_lane=? AND NOT EXISTS (SELECT 1 FROM messages child WHERE child.parent_id=messages.message_id) AND (state='accepted' OR (state IN ('queued','delivered') AND expires_at>?)) ORDER BY created_at,message_id LIMIT ?", (data['lane_id'], now, limit)).fetchall()
        if not rows:
            return {'claimed': False, 'reason': 'empty', 'messages': []}
        claim_id = uid()
        con.execute('INSERT INTO claims VALUES(?,?,?,?,?,NULL)', (claim_id, data['lane_id'], data['consumer_id'], now, now + lease))
        for row in rows:
            con.execute("UPDATE messages SET state=CASE WHEN state='queued' THEN 'delivered' ELSE state END,delivered_at=COALESCE(delivered_at,?),claim_id=? WHERE message_id=?", (now, claim_id, row['message_id']))
        return {'claimed': True, 'claim_id': claim_id, 'expires_at': now + lease, 'messages': [self.message_record(r) for r in con.execute('SELECT * FROM messages WHERE claim_id=? ORDER BY created_at,message_id', (claim_id,))]}

    def active_claim(self, con, data, now):
        self.consumer(con, data, now)
        required(data, 'claim_id')
        row = con.execute('SELECT * FROM claims WHERE claim_id=?', (data['claim_id'],)).fetchone()
        if not row or row['lane_id'] != data['lane_id'] or row['consumer_id'] != data['consumer_id'] or row['expires_at'] <= now or row['released_at'] is not None:
            raise DeskError('claim lease is missing, expired, released, or belongs to another consumer')
        return row

    def inbox_renew(self, con, data, now):
        self.active_claim(con, data, now)
        lease = bounded(data, 'lease_seconds', 120, 10, 300)
        con.execute('UPDATE claims SET expires_at=? WHERE claim_id=?', (now + lease, data['claim_id']))
        con.execute('UPDATE consumers SET heartbeat_at=?,expires_at=MAX(expires_at,?) WHERE lane_id=?', (now, now + lease, data['lane_id']))
        return {'claim_id': data['claim_id'], 'expires_at': now + lease}

    def inbox_release(self, con, data, now):
        self.active_claim(con, data, now)
        con.execute('UPDATE claims SET released_at=? WHERE claim_id=?', (now, data['claim_id']))
        return {'released': True, 'claim_id': data['claim_id']}

    def message_transition(self, con, data, now, target):
        self.active_claim(con, data, now)
        required(data, 'message_id')
        row = con.execute('SELECT * FROM messages WHERE message_id=?', (data['message_id'],)).fetchone()
        if not row or row['to_lane'] != data['lane_id'] or row['claim_id'] != data['claim_id'] or (row['expires_at'] <= now and row['state'] not in ('accepted', 'resolved')):
            raise DeskError('message is absent, expired, or not in this claim')
        if row['state'] == target:
            return self.message_record(row)
        if row['state'] != ('delivered' if target == 'accepted' else 'accepted'):
            raise DeskError('resolution requires acceptance; acceptance requires delivery')
        column = 'accepted_at' if target == 'accepted' else 'resolved_at'
        con.execute(f'UPDATE messages SET state=?,{column}=?,resolution=? WHERE message_id=?',
                    (target, now, data.get('resolution') if target == 'resolved' else None, data['message_id']))
        return self.message_record(con.execute('SELECT * FROM messages WHERE message_id=?', (data['message_id'],)).fetchone())

    def message_accept(self, con, data, now):
        return self.message_transition(con, data, now, 'accepted')

    def message_resolve(self, con, data, now):
        return self.message_transition(con, data, now, 'resolved')

    def incident_report(self, con, data, now):
        required(data, 'repo', 'build', 'reporter_lane', 'owner_lane', 'evidence')
        self.lane(con, data['reporter_lane'])
        self.lane(con, data['owner_lane'])
        row = con.execute('SELECT * FROM incidents WHERE repo=? AND build=?', (data['repo'], str(data['build']))).fetchone()
        first = row is None
        if first:
            incident_id = uid()
            con.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,NULL,NULL)',
                        (incident_id, data['repo'], str(data['build']), data['owner_lane'], 'open', now))
        else:
            incident_id = row['incident_id']
        owner = data['owner_lane'] if first else row['owner_lane']
        topic = 'incident:' + incident_id
        self.subscribe(con, {'lane_id': data['reporter_lane'], 'topic': topic}, now)
        evidence_hash = hashlib.sha256(str(data['evidence']).strip().encode()).hexdigest()
        added = con.execute('INSERT OR IGNORE INTO incident_evidence VALUES(?,?,?,?,?)',
                            (incident_id, evidence_hash, data['evidence'], data['reporter_lane'], now)).rowcount > 0
        if first:
            self.message_send(con, {'from_lane': data['reporter_lane'], 'to_lane': owner, 'kind': 'work_request',
                'dedup_key': 'incident:' + incident_id + ':work', 'body': json.dumps({'incident_id': incident_id, 'repo': data['repo'], 'build': str(data['build']), 'evidence': data['evidence']}),
                'ttl_seconds': data.get('ttl_seconds', 86400)}, now)
        if not first and added and row['state'] == 'open':
            self.message_send(con, {'from_lane': data['reporter_lane'], 'to_lane': owner, 'kind': 'update',
                'dedup_key': 'incident:' + incident_id + ':evidence:' + evidence_hash,
                'body': json.dumps({'incident_id': incident_id, 'evidence': data['evidence']}),
                'ttl_seconds': data.get('ttl_seconds', 86400)}, now)
        if row and row['state'] == 'resolved':
            self.incident_notify(con, dict(row), data['reporter_lane'], now)
        result = dict(con.execute('SELECT * FROM incidents WHERE incident_id=?', (incident_id,)).fetchone())
        return dict(result, created=first, evidence_added=added)

    def incident_notify(self, con, row, lane_id, now):
        return self.message_send(con, {'from_lane': row['owner_lane'], 'to_lane': lane_id, 'kind': 'update',
            'dedup_key': 'incident:' + row['incident_id'] + ':resolved:' + lane_id,
            'body': json.dumps({'incident_id': row['incident_id'], 'resolution': row['resolution']})}, now)

    def incident_resolve(self, con, data, now):
        required(data, 'incident_id', 'owner_lane', 'resolution')
        row = con.execute('SELECT * FROM incidents WHERE incident_id=?', (data['incident_id'],)).fetchone()
        if not row or row['owner_lane'] != data['owner_lane']:
            raise DeskError('only the incident owner may resolve it')
        if row['state'] == 'resolved' and row['resolution'] != data['resolution']:
            raise DeskError('incident already resolved with different evidence')
        con.execute('UPDATE incidents SET state=?,resolved_at=COALESCE(resolved_at,?),resolution=? WHERE incident_id=?', ('resolved', now, data['resolution'], data['incident_id']))
        result = dict(con.execute('SELECT * FROM incidents WHERE incident_id=?', (data['incident_id'],)).fetchone())
        reporters = con.execute('SELECT lane_id FROM subscriptions WHERE topic=? AND active=1', ('incident:' + data['incident_id'],)).fetchall()
        for reporter in reporters:
            self.incident_notify(con, result, reporter['lane_id'], now)
        return result

    def pause(self, con, data, now):
        if not isinstance(data.get('paused'), bool):
            raise DeskError('paused must be boolean')
        con.execute("INSERT OR REPLACE INTO settings VALUES('paused',?)", (json.dumps(data['paused']),))
        return {'paused': data['paused']}

    def snapshot(self, con, data, now):
        limit = bounded(data, 'limit', 200, 1, 1000)
        tables = ('lanes', 'prs', 'messages', 'incidents', 'subscriptions', 'workstreams', 'requests')
        lane_id = data.get('lane_id')
        if lane_id is not None:
            required(data, 'lane_id')
        result = {'schema_version': 1, 'paused': False, 'lanes': [], 'prs': [], 'messages': [], 'incidents': [], 'subscriptions': [], 'workstreams': [], 'requests': [],
                  'counts': {table: 0 for table in tables}, 'truncated': {table: False for table in tables}, 'limit': limit}
        if self.path.resolve() == DEFAULT_DB.resolve():
            try:
                health = json.loads((STATE_DIR / 'dispatcher-status.json').read_text())
                result['watcher'] = {key: health.get(key) for key in ('status', 'delivery', 'pending_events')}
                result['watcher']['fresh'] = 0 <= now - health['checked_at'] < 180
            except (OSError, ValueError, KeyError, TypeError):
                result['watcher'] = {'status': 'unavailable', 'delivery': 'unavailable', 'fresh': False}
        if lane_id is not None:
            result['lane_id'] = lane_id
        request_list = self.request_list(con, {'owner_lane': lane_id, 'request_history_limit': data.get('request_history_limit', 100)}, now)
        result['requests'] = request_list['requests']
        result['counts'].update(request_list['counts'])
        result['truncated'].update(request_list['truncated'])
        result['request_history_limit'] = request_list['request_history_limit']
        self.project_snapshot(con, data, now, result)
        if con is None:
            if lane_id is not None:
                raise DeskError('lane is not registered: ' + str(lane_id))
            return result
        if lane_id is not None:
            self.lane(con, lane_id)
        result['paused'] = self.is_paused(con)
        scope = {
            'lanes': 'lane_id=:lane_id',
            'workstreams': 'lane_id=:lane_id',
            'prs': "owner_lane=:lane_id OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.lane_id=:lane_id AND s.topic='pr:' || prs.repo || ':' || prs.pr)",
            'messages': 'from_lane=:lane_id OR to_lane=:lane_id',
            'incidents': "owner_lane=:lane_id OR EXISTS (SELECT 1 FROM subscriptions s WHERE s.lane_id=:lane_id AND s.topic='incident:' || incidents.incident_id)",
            'subscriptions': 'lane_id=:lane_id',
        }
        active_lane = """(
            EXISTS (SELECT 1 FROM consumers c WHERE c.lane_id=lanes.lane_id AND c.expires_at>:now)
            OR EXISTS (SELECT 1 FROM prs p WHERE p.owner_lane=lanes.lane_id AND p.status NOT IN ('promoted','abandoned','superseded'))
            OR EXISTS (SELECT 1 FROM messages m WHERE (m.to_lane=lanes.lane_id OR m.from_lane=lanes.lane_id)
                       AND NOT EXISTS (SELECT 1 FROM messages child WHERE child.parent_id=m.message_id)
                       AND (m.state='accepted' OR (m.state IN ('queued','delivered') AND (m.expires_at>:now OR EXISTS (SELECT 1 FROM lanes h WHERE h.lane_id=m.to_lane AND h.provider='human')))))
        ) DESC,created_at DESC,lane_id"""
        orders = {
            'lanes': active_lane,
            'workstreams': 'updated_at DESC,workstream_key',
            'prs': "(status='promoted'),updated_at DESC,repo,pr",
            'messages': "(state='resolved' OR EXISTS (SELECT 1 FROM messages child WHERE child.parent_id=messages.message_id) OR (state!='accepted' AND expires_at<=:now AND NOT EXISTS (SELECT 1 FROM lanes human WHERE human.lane_id=messages.to_lane AND human.provider='human'))),rowid DESC",
            'incidents': "(state='resolved'),created_at DESC,incident_id",
            'subscriptions': 'active DESC,created_at DESC,lane_id,topic',
        }
        params = {'lane_id': lane_id, 'now': now, 'limit': limit}
        existing_tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in tables:
            if table == 'requests':
                continue  # Request coverage is independent of the message/page limit.
            if table == 'workstreams' and table not in existing_tables:
                continue  # Read-only snapshots of old ledgers must never migrate.
            where = ' WHERE (' + scope[table] + ')' if lane_id is not None else ''
            result['counts'][table] = con.execute(f'SELECT count(*) FROM {table}{where}', params).fetchone()[0]
            result[table] = [dict(row) for row in con.execute(f'SELECT * FROM {table}{where} ORDER BY {orders[table]} LIMIT :limit', params)]
            result['truncated'][table] = result['counts'][table] > len(result[table])
        projected = self.conversation_projection(con, now)
        for message in result['messages']:
            message.update(projected[message['message_id']])
        consumers = {r['lane_id']: dict(r) for r in con.execute('SELECT * FROM consumers')}
        for lane in result['lanes']:
            consumer = consumers.get(lane['lane_id'])
            if consumer:
                consumer['metadata'] = json.loads(consumer['metadata'])
            lane['consumer'] = consumer
            lane['connected'] = bool(consumer and consumer['expires_at'] > now)
            lane['connection_status'] = 'connected' if lane['connected'] else 'disconnected'
            lane['pending_count'] = sum(m['pending'] and m['to_lane'] == lane['lane_id'] for m in projected.values())
        for pr in result['prs']:
            for field in ('workstream_key', 'title', 'opened_by_lane'):
                pr.setdefault(field, None)
            pr['observed_status'] = json.loads(pr['observed_status']) if pr.get('observed_status') else None
            pr['closure'] = json.loads(pr['closure']) if pr.get('closure') else None
            pr['closed'] = pr['status'] in TERMINAL_PR_STATES
            pr['attestation'] = json.loads(pr['attestation']) if pr['attestation'] else None
        for incident in result['incidents']:
            incident['evidence'] = [dict(r) for r in con.execute('SELECT * FROM incident_evidence WHERE incident_id=? ORDER BY created_at', (incident['incident_id'],))]
            incident['reporters'] = [r[0] for r in con.execute('SELECT lane_id FROM subscriptions WHERE topic=? AND active=1', ('incident:' + incident['incident_id'],))]
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog='Payloads are assertions by trusted local agents, not authenticated external evidence. Global pause gates new inbox claims; it does not cancel accepted work. Ledger commands do not send external notifications or wake providers.')
    parser.add_argument('--db', default=str(DEFAULT_DB), help='SQLite ledger path (default in local state directory)')
    sub = parser.add_subparsers(dest='command', required=True)
    for command, contract in CONTRACT.items():
        child = sub.add_parser(command, help=contract, description='JSON payload fields: ' + contract)
        child.add_argument('--json', default='{}', help='one JSON object; see payload fields above')
    args = parser.parse_args(argv)
    try:
        payload = json.loads(args.json)
        result = Desk(args.db).execute(args.command, payload)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (DeskError, sqlite3.Error, OSError, TypeError, json.JSONDecodeError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
