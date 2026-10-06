"""Additive project/arc narratives and exact CoS promotion receipts for Desk.

Commands assert trusted local facts inside Desk's existing write transaction.
No external board rows are inferred, no external operations run, and neither promotion nor
inbox state completes a card or arc. Read-only projections never migrate data.
"""
import hashlib
import json
import math
import re


PROJECT_CONTRACT = {
    'project-upsert': 'project_id (stable slug), name, expected_version (0 creates; exact version updates)',
    'arc-upsert': 'arc_id (stable slug), project_id, owner_lane (registered nonhuman), title, summary (one sentence), state (planned|working|waiting|paused|completed), current_state, story_so_far, next_steps, done_when, direction, reference, expected_version. Named owner is explicitly enrolled. Existing owner changes also require expected_owner and ownership_reason; project is immutable',
    'arc-join': 'arc_id, lane_id (actual registered nonhuman participant), direction, reference, expected_version (0 creates membership; exact current membership version updates)',
    'card-upsert': 'workstream_key (existing workstream-bind), arc_id, owner_lane (matching binding and arc member), title, state (unknown|planned|working|waiting|paused|completed), summary, waiting_on (may be empty except when waiting), next_step, expected_version. Unknown explicitly records unconfirmed lifecycle; never map board ready to completed. Optional pr_keys:[{repo,pr}], message_ids:[existing message IDs]; omission preserves attachments. Optional reported_at (UTC epoch <=now) preserves original report freshness on import; meaningful updates default to now. Owner changes also require expected_owner and ownership_reason; arc moves require expected_arc_id and move_reason',
    'promotion-batch': 'batch_id (stable slug), expected_version:0 (immutable receipt; identical retry is idempotent), project_id, repo, target, image, deployed_commit, cos_lane, included_prs:[{pr,expected_version,merge_commit}], attestation:{included_commits:[every merge and deployed commit],runtime_evidence:[references]}. Exact all-or-nothing CoS attestation, no notifications or external operations',
}

PROJECT_SCHEMA = '''
CREATE TABLE IF NOT EXISTS projects (
 project_id TEXT PRIMARY KEY, name TEXT NOT NULL, version INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS arcs (
 arc_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id),
 owner_lane TEXT NOT NULL REFERENCES lanes(lane_id), title TEXT NOT NULL,
 summary TEXT NOT NULL, state TEXT NOT NULL, current_state TEXT NOT NULL,
 story_so_far TEXT NOT NULL, next_steps TEXT NOT NULL, done_when TEXT NOT NULL,
 direction TEXT NOT NULL, reference TEXT NOT NULL, version INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL);
CREATE INDEX IF NOT EXISTS ix_arcs_project ON arcs(project_id,updated_at);
CREATE TABLE IF NOT EXISTS arc_members (
 arc_id TEXT NOT NULL REFERENCES arcs(arc_id), lane_id TEXT NOT NULL REFERENCES lanes(lane_id),
 direction TEXT NOT NULL, reference TEXT NOT NULL, version INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(arc_id,lane_id));
CREATE TABLE IF NOT EXISTS cards (
 workstream_key TEXT PRIMARY KEY REFERENCES workstreams(workstream_key),
 arc_id TEXT NOT NULL REFERENCES arcs(arc_id), owner_lane TEXT NOT NULL REFERENCES lanes(lane_id),
 title TEXT NOT NULL, state TEXT NOT NULL, summary TEXT NOT NULL,
 waiting_on TEXT NOT NULL, next_step TEXT NOT NULL, pr_keys TEXT NOT NULL,
 message_ids TEXT NOT NULL, version INTEGER NOT NULL,
 created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL, reported_at REAL);
CREATE INDEX IF NOT EXISTS ix_cards_arc ON cards(arc_id,updated_at);
CREATE TABLE IF NOT EXISTS arc_history (
 arc_id TEXT NOT NULL REFERENCES arcs(arc_id), version INTEGER NOT NULL,
 changed_at REAL NOT NULL, snapshot TEXT NOT NULL, PRIMARY KEY(arc_id,version));
CREATE TABLE IF NOT EXISTS promotion_batches (
 batch_id TEXT PRIMARY KEY, version INTEGER NOT NULL, fingerprint TEXT NOT NULL UNIQUE,
 project_id TEXT NOT NULL REFERENCES projects(project_id), repo TEXT NOT NULL,
 target TEXT NOT NULL, image TEXT NOT NULL, deployed_commit TEXT NOT NULL,
 cos_lane TEXT NOT NULL REFERENCES lanes(lane_id), included_prs TEXT NOT NULL,
 attestation TEXT NOT NULL, evidence TEXT NOT NULL, verified_at REAL NOT NULL,
 created_at REAL NOT NULL);
'''

PROJECT_TABLES = ('projects', 'arcs', 'arc_members', 'cards', 'arc_history', 'promotion_batches')
STATES = ('planned', 'working', 'waiting', 'paused', 'completed')
CARD_STATES = ('unknown', *STATES)
ARC_FIELDS = ('project_id', 'owner_lane', 'title', 'summary', 'state', 'current_state',
              'story_so_far', 'next_steps', 'done_when', 'direction', 'reference')
CARD_FIELDS = ('arc_id', 'owner_lane', 'title', 'state', 'summary', 'waiting_on',
               'next_step', 'pr_keys', 'message_ids')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class ProjectMixin:
    def _project_error(self, message):
        return self.project_error_type(message)

    def _project_text(self, data, key, maximum=4000, empty=False):
        value = data.get(key)
        if (not isinstance(value, str) or '\0' in value or len(value) > maximum
                or (not empty and not value.strip())):
            raise self._project_error(key + ' must be ' + ('a' if empty else 'a nonempty') + ' string (maximum ' + str(maximum) + ')')
        return value.strip()

    def _project_slug(self, data, key):
        value = self._project_text(data, key, 200)
        if value != data[key] or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', value):
            raise self._project_error(key + ' must be a stable lowercase slug')
        return value

    def _project_version(self, data, key='expected_version'):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 9223372036854775807:
            raise self._project_error(key + ' must be a nonnegative integer')
        return value

    def _project_cas(self, row, data, kind):
        if self._project_version(data) != (row['version'] if row else 0):
            raise self._project_error('stale ' + kind + ' version')

    def _project_lane(self, con, data, key):
        lane_id = self._project_text(data, key, 200)
        lane = self.lane(con, lane_id)
        if lane['provider'].casefold() == 'human':
            raise self._project_error(key + ' must be a registered nonhuman lane')
        return lane_id

    def _project_row(self, con, table, key, value):
        # Table/key are internal literals, never values supplied by a caller.
        row = con.execute(f'SELECT * FROM {table} WHERE {key}=?', (value,)).fetchone()
        if not row:
            raise self._project_error(table.rstrip('s') + ' does not exist: ' + value)
        return dict(row)

    def _project_state(self, data, card=False):
        allowed = CARD_STATES if card else STATES
        if data.get('state') not in allowed:
            raise self._project_error('state must be one of ' + ', '.join(allowed))
        return data['state']

    def _project_summary(self, data):
        summary = self._project_text(data, 'summary', 1000)
        # Do not attempt linguistic inference (abbreviations and URLs are valid).
        if '\n' in summary or '\r' in summary:
            raise self._project_error('summary must be a single sentence on one line')
        return summary

    def _project_transfer(self, row, data):
        if row and row['owner_lane'] != data['owner_lane']:
            if data.get('expected_owner') != row['owner_lane']:
                raise self._project_error('ownership change requires exact expected_owner')
            self._project_text(data, 'ownership_reason')

    def project_upsert(self, con, data, now):
        project_id = self._project_slug(data, 'project_id')
        name = self._project_text(data, 'name', 200)
        row = con.execute('SELECT * FROM projects WHERE project_id=?', (project_id,)).fetchone()
        self._project_cas(row, data, 'project')
        if not row:
            con.execute('INSERT INTO projects VALUES(?,?,1,?,?)', (project_id, name, now, now))
        elif row['name'] != name:
            con.execute('UPDATE projects SET name=?,version=version+1,updated_at=? WHERE project_id=?', (name, now, project_id))
        return self._project_row(con, 'projects', 'project_id', project_id)

    def arc_upsert(self, con, data, now):
        arc_id = self._project_slug(data, 'arc_id')
        project_id = self._project_slug(data, 'project_id')
        self._project_row(con, 'projects', 'project_id', project_id)
        payload = {key: self._project_text(data, key, 20000 if key in ('current_state', 'story_so_far', 'next_steps', 'done_when') else 4000)
                   for key in ARC_FIELDS}
        payload.update(project_id=project_id, owner_lane=self._project_lane(con, data, 'owner_lane'),
                       title=self._project_text(data, 'title', 500), summary=self._project_summary(data), state=self._project_state(data))
        row = con.execute('SELECT * FROM arcs WHERE arc_id=?', (arc_id,)).fetchone()
        self._project_cas(row, data, 'arc')
        self._project_transfer(row, payload | {key: data[key] for key in ('expected_owner', 'ownership_reason') if key in data})
        if row and row['project_id'] != project_id:
            raise self._project_error('arc project_id is immutable')
        changed = not row or any(row[key] != payload[key] for key in ARC_FIELDS)
        if changed:
            completed_at = (row['completed_at'] if row and row['state'] == 'completed' else now) if payload['state'] == 'completed' else None
            if row:
                con.execute('UPDATE arcs SET ' + ','.join(key + '=?' for key in ARC_FIELDS) + ',version=version+1,updated_at=?,completed_at=? WHERE arc_id=?',
                            (*payload.values(), now, completed_at, arc_id))
            else:
                con.execute('INSERT INTO arcs(arc_id,' + ','.join(ARC_FIELDS) + ',version,created_at,updated_at,completed_at) VALUES(' + ','.join('?' for _ in range(len(ARC_FIELDS) + 5)) + ')',
                            (arc_id, *payload.values(), 1, now, now, completed_at))
            current = self._project_row(con, 'arcs', 'arc_id', arc_id)
            con.execute('INSERT INTO arc_history VALUES(?,?,?,?)', (arc_id, current['version'], now, canonical(current)))
        # Naming the arc owner is an explicit participation assertion. Other
        # lanes join only through arc-join; no roster or label heuristics.
        if not con.execute('SELECT 1 FROM arc_members WHERE arc_id=? AND lane_id=?', (arc_id, payload['owner_lane'])).fetchone():
            self.arc_join(con, dict(arc_id=arc_id, lane_id=payload['owner_lane'], direction=payload['direction'],
                                   reference=payload['reference'], expected_version=0), now)
        return self._project_row(con, 'arcs', 'arc_id', arc_id)

    def arc_join(self, con, data, now):
        arc_id = self._project_slug(data, 'arc_id')
        self._project_row(con, 'arcs', 'arc_id', arc_id)
        lane_id = self._project_lane(con, data, 'lane_id')
        direction, reference = (self._project_text(data, key) for key in ('direction', 'reference'))
        row = con.execute('SELECT * FROM arc_members WHERE arc_id=? AND lane_id=?', (arc_id, lane_id)).fetchone()
        self._project_cas(row, data, 'arc membership')
        if not row:
            con.execute('INSERT INTO arc_members VALUES(?,?,?,?,1,?,?)', (arc_id, lane_id, direction, reference, now, now))
        elif row['direction'] != direction or row['reference'] != reference:
            con.execute('UPDATE arc_members SET direction=?,reference=?,version=version+1,updated_at=? WHERE arc_id=? AND lane_id=?',
                        (direction, reference, now, arc_id, lane_id))
        return dict(con.execute('SELECT * FROM arc_members WHERE arc_id=? AND lane_id=?', (arc_id, lane_id)).fetchone())

    def _card_attachments(self, con, data, row):
        result = {}
        for name in ('pr_keys', 'message_ids'):
            values = data.get(name, json.loads(row[name]) if row else [])
            if not isinstance(values, list) or len(values) > 1000:
                raise self._project_error(name + ' must be a list of at most 1000 existing references')
            checked = []
            for value in values:
                if name == 'pr_keys':
                    if not isinstance(value, dict) or set(value) != {'repo', 'pr'}:
                        raise self._project_error('pr_keys entries require exactly repo and pr')
                    repo = self._project_text(value, 'repo', 1000)
                    if isinstance(value.get('pr'), bool) or not isinstance(value.get('pr'), (str, int)) or not str(value['pr']).strip():
                        raise self._project_error('pr must be a nonempty PR key')
                    normalized = {'repo': repo, 'pr': str(value['pr'])}
                    linked = self.get_pr(con, normalized)
                else:
                    normalized = self._project_text({'message_id': value}, 'message_id', 200)
                    linked = self._project_row(con, 'messages', 'message_id', normalized)
                if linked.get('workstream_key') and linked['workstream_key'] != data['workstream_key']:
                    raise self._project_error('attachment belongs to another workstream')
                if normalized in checked:
                    raise self._project_error('duplicate ' + name + ' reference')
                checked.append(normalized)
            result[name] = canonical(sorted(checked, key=canonical))
        return result

    def _card_result(self, con, key):
        row = self._project_row(con, 'cards', 'workstream_key', key)
        row['project_id'] = self._project_row(con, 'arcs', 'arc_id', row['arc_id'])['project_id']
        for name in ('pr_keys', 'message_ids'):
            row[name] = json.loads(row[name])
        return row

    def card_upsert(self, con, data, now):
        key = self._project_slug(data, 'workstream_key')
        arc_id = self._project_slug(data, 'arc_id')
        self._project_row(con, 'arcs', 'arc_id', arc_id)
        owner = self._project_lane(con, data, 'owner_lane')
        binding = self._project_row(con, 'workstreams', 'workstream_key', key)
        if binding['lane_id'] != owner:
            raise self._project_error('card owner must match the current workstream binding')
        if not con.execute('SELECT 1 FROM arc_members WHERE arc_id=? AND lane_id=?', (arc_id, owner)).fetchone():
            raise self._project_error('card owner must explicitly join the arc first')
        row = con.execute('SELECT * FROM cards WHERE workstream_key=?', (key,)).fetchone()
        self._project_cas(row, data, 'card')
        self._project_transfer(row, data)
        if row and row['arc_id'] != arc_id:
            if data.get('expected_arc_id') != row['arc_id']:
                raise self._project_error('card move requires exact expected_arc_id')
            self._project_text(data, 'move_reason')
        state = self._project_state(data, card=True)
        payload = dict(arc_id=arc_id, owner_lane=owner, title=self._project_text(data, 'title', 500),
                       state=state, summary=self._project_summary(data), waiting_on=self._project_text(data, 'waiting_on', empty=state != 'waiting'),
                       next_step=self._project_text(data, 'next_step'))
        payload.update(self._card_attachments(con, data, row))
        reported_at = data.get('reported_at', now)
        if isinstance(reported_at, bool) or not isinstance(reported_at, (int, float)) or not math.isfinite(reported_at) or not 0 <= reported_at <= now:
            raise self._project_error('reported_at must be UTC epoch seconds between 0 and now')
        changed = not row or any(row[name] != payload[name] for name in CARD_FIELDS)
        if changed or ('reported_at' in data and row['reported_at'] != reported_at):
            completed_at = (row['completed_at'] if row and row['state'] == 'completed' else now) if state == 'completed' else None
            if row:
                con.execute('UPDATE cards SET ' + ','.join(name + '=?' for name in CARD_FIELDS) + ',version=version+1,updated_at=?,completed_at=?,reported_at=? WHERE workstream_key=?',
                            (*payload.values(), now, completed_at, reported_at, key))
            else:
                con.execute('INSERT INTO cards(workstream_key,' + ','.join(CARD_FIELDS) + ',version,created_at,updated_at,completed_at,reported_at) VALUES(' + ','.join('?' for _ in range(len(CARD_FIELDS) + 6)) + ')',
                            (key, *payload.values(), 1, now, now, completed_at, reported_at))
        return self._card_result(con, key)

    def promotion_batch(self, con, data, now):
        batch_id = self._project_slug(data, 'batch_id')
        if self._project_version(data) != 0:
            raise self._project_error('promotion batch is immutable; expected_version must be 0')
        project_id = self._project_slug(data, 'project_id')
        self._project_row(con, 'projects', 'project_id', project_id)
        cos_lane = self._project_lane(con, data, 'cos_lane')
        if self.lane(con, cos_lane)['role'] != 'cos':
            raise self._project_error('promotion requires a registered CoS lane')
        payload = {key: self._project_text(data, key, 2000) for key in ('repo', 'target', 'image', 'deployed_commit')}
        payload['repo'] = self.canonical_pr_payload(payload)['repo']
        payload.update(project_id=project_id, cos_lane=cos_lane)
        entries = data.get('included_prs')
        if not isinstance(entries, list) or not 1 <= len(entries) <= 1000:
            raise self._project_error('included_prs requires 1..1000 exact PR records')
        normalized = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {'pr', 'expected_version', 'merge_commit'}:
                raise self._project_error('included_prs entries require exactly pr, expected_version and merge_commit')
            if isinstance(entry['pr'], bool) or not isinstance(entry['pr'], (str, int)) or not str(entry['pr']).strip():
                raise self._project_error('pr must be a nonempty PR key')
            pr = str(entry['pr'])
            if pr in seen:
                raise self._project_error('included_prs contains a duplicate PR')
            seen.add(pr)
            normalized.append(dict(repo=payload['repo'], pr=pr, expected_version=self._project_version(entry),
                                   merge_commit=self._project_text(entry, 'merge_commit', 2000)))
        payload['included_prs'] = sorted(normalized, key=lambda entry: entry['pr'])
        att = data.get('attestation')
        if not isinstance(att, dict) or set(att) != {'included_commits', 'runtime_evidence'}:
            raise self._project_error('attestation requires exactly included_commits and runtime_evidence')
        for field in ('included_commits', 'runtime_evidence'):
            values = att[field]
            if not isinstance(values, list) or not 1 <= len(values) <= 10000 or any(not isinstance(v, str) or not v.strip() or '\0' in v or len(v) > 10000 for v in values):
                raise self._project_error(field + ' must contain nonempty evidence references')
        required_commits = {payload['deployed_commit'], *(entry['merge_commit'] for entry in normalized)}
        if not required_commits.issubset(set(att['included_commits'])):
            raise self._project_error('attestation must include every merge commit and the deployed commit')
        payload['attestation'] = {field: sorted(set(att[field])) for field in ('included_commits', 'runtime_evidence')}
        fingerprint = hashlib.sha256(canonical(payload).encode()).hexdigest()
        existing = con.execute('SELECT * FROM promotion_batches WHERE batch_id=? OR fingerprint=?', (batch_id, fingerprint)).fetchall()
        if existing:
            if len(existing) != 1 or existing[0]['fingerprint'] != fingerprint:
                raise self._project_error('promotion batch id already records different evidence')
            return self._promotion_result(dict(existing[0]))
        # Validate the complete set before calling the existing transition helpers.
        rows = []
        for entry in payload['included_prs']:
            row = self.get_pr(con, entry)
            if row['version'] != entry['expected_version'] or row['status'] not in ('owned', 'merged'):
                raise self._project_error('promotion batch requires each current owned or merged PR version')
            if row['status'] == 'merged' and row['merge_commit'] != entry['merge_commit']:
                raise self._project_error('promotion merge_commit does not match recorded merge')
            # When a PR already has explicit project membership, reject a wrong
            # project receipt. Legacy PRs without cards remain valid assertions.
            card = con.execute('SELECT a.project_id FROM cards c JOIN arcs a ON a.arc_id=c.arc_id WHERE c.workstream_key=?', (row['workstream_key'],)).fetchone()
            if card and card['project_id'] != project_id:
                raise self._project_error('included PR belongs to a different project')
            rows.append(row)
        evidence = payload['attestation']['runtime_evidence']
        promotion_att = dict(payload['attestation'], release=payload['target'], deployed_commit=payload['deployed_commit'],
                             project_id=project_id, repo=payload['repo'], target=payload['target'], image=payload['image'], batch_id=batch_id)
        included = []
        for row, entry in zip(rows, payload['included_prs']):
            if row['status'] == 'owned':
                row = self.pr_merge(con, dict(repo=row['repo'], pr=row['pr'], owner_lane=row['owner_lane'],
                                             expected_version=row['version'], merge_commit=entry['merge_commit']), now)
            promoted = self.pr_promote(con, dict(repo=row['repo'], pr=row['pr'], cos_lane=cos_lane,
                                                expected_version=row['version'], attestation=promotion_att, notify_owner=False), now)
            included.append(dict(entry, promoted_version=promoted['version'], owner_lane=promoted['owner_lane'], opened_by_lane=promoted['opened_by_lane']))
        con.execute('INSERT INTO promotion_batches VALUES(?,1,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (batch_id, fingerprint, project_id, payload['repo'], payload['target'], payload['image'], payload['deployed_commit'], cos_lane,
                     canonical(included), canonical(dict(promotion_att, cos_lane=cos_lane, attested_at=now)), canonical(evidence), now, now))
        self._lifecycle_reconcile_requests(con, now)
        return self._promotion_result(self._project_row(con, 'promotion_batches', 'batch_id', batch_id))

    def _promotion_result(self, row):
        for field in ('included_prs', 'attestation', 'evidence'):
            row[field] = json.loads(row[field])
        return row

    def card_message_context(self, con, workstream_key, now):
        """Capture explicit ledger identity once, never routing or authority."""
        if not workstream_key:
            return None
        row = con.execute('''SELECT c.workstream_key,c.title AS card_title,c.version AS card_version,c.pr_keys,
            a.arc_id,a.title AS arc_title,a.version AS arc_version,p.project_id,p.name AS project_name
            FROM cards c JOIN workstreams w ON w.workstream_key=c.workstream_key
            JOIN arcs a ON a.arc_id=c.arc_id JOIN projects p ON p.project_id=a.project_id
            WHERE c.workstream_key=?''', (workstream_key,)).fetchone()
        if not row:
            return None
        keys = {(entry['repo'], entry['pr']) for entry in json.loads(row['pr_keys'])}
        keys.update((entry['repo'], entry['pr']) for entry in con.execute('SELECT repo,pr FROM prs WHERE workstream_key=?', (workstream_key,)))
        prs = []
        for repo, pr in sorted(keys):
            linked = con.execute('SELECT repo,pr,url FROM prs WHERE repo=? AND pr=?', (repo, pr)).fetchone()
            if linked:
                prs.append(dict(linked))
        return dict(schema_version=1, project=dict(project_id=row['project_id'], name=row['project_name']),
                    arc=dict(arc_id=row['arc_id'], title=row['arc_title'], version=row['arc_version']),
                    card=dict(workstream_key=row['workstream_key'], title=row['card_title'], version=row['card_version']),
                    prs=prs, captured_at=now)

    def project_snapshot(self, con, data, now, result):
        history_limit = data.get('arc_history_limit', result['limit'])
        if isinstance(history_limit, bool) or not isinstance(history_limit, int) or not 0 <= history_limit <= 1000:
            raise self._project_error('arc_history_limit must be an integer in 0..1000')
        since = data.get('arc_history_since', 0)
        if isinstance(since, bool) or not isinstance(since, (int, float)) or not math.isfinite(since) or since < 0:
            raise self._project_error('arc_history_since must be nonnegative UTC epoch seconds')
        result['arc_history_limit'] = history_limit
        result['arc_history_since'] = since
        for table in PROJECT_TABLES:
            result[table] = []
            result['counts'][table] = 0
            result['truncated'][table] = False
        if con is None:
            return
        existing = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lane_id = data.get('lane_id')
        # Missing additive tables in an old ledger are empty, never inferred.
        arc_scope = 'SELECT arc_id FROM arcs WHERE owner_lane=:lane_id'
        if 'arc_members' in existing:
            arc_scope += ' UNION SELECT arc_id FROM arc_members WHERE lane_id=:lane_id'
        scopes = {
            'projects': 'project_id IN (SELECT project_id FROM arcs WHERE arc_id IN (' + arc_scope + '))',
            'arcs': 'arc_id IN (' + arc_scope + ')',
            'arc_members': 'arc_id IN (' + arc_scope + ')',
            'cards': 'arc_id IN (' + arc_scope + ')',
            'arc_history': 'arc_id IN (' + arc_scope + ')',
            'promotion_batches': 'cos_lane=:lane_id OR project_id IN (SELECT project_id FROM arcs WHERE arc_id IN (' + arc_scope + '))'
                                 + " OR EXISTS (SELECT 1 FROM json_each(promotion_batches.included_prs) p WHERE json_extract(p.value,'$.owner_lane')=:lane_id OR json_extract(p.value,'$.opened_by_lane')=:lane_id)",
        }
        orders = {'projects': 'updated_at DESC,project_id', 'arcs': 'updated_at DESC,arc_id',
                  'arc_members': 'updated_at DESC,arc_id,lane_id', 'cards': 'updated_at DESC,workstream_key',
                  'arc_history': 'changed_at DESC,arc_id,version DESC', 'promotion_batches': 'verified_at DESC,batch_id'}
        for table in PROJECT_TABLES:
            if table not in existing or (lane_id is not None and table != 'arcs' and 'arcs' not in existing):
                continue
            conditions = ['(' + scopes[table] + ')'] if lane_id is not None else []
            if table == 'arc_history':
                conditions.append('changed_at>=:since')
            where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
            params = dict(lane_id=lane_id, since=since, limit=history_limit if table == 'arc_history' else result['limit'])
            result['counts'][table] = con.execute(f'SELECT count(*) FROM {table}' + where, params).fetchone()[0]
            result[table] = [dict(row) for row in con.execute(f'SELECT * FROM {table}' + where + ' ORDER BY ' + orders[table] + ' LIMIT :limit', params)]
            result['truncated'][table] = result['counts'][table] > len(result[table])
        for card in result['cards']:
            card.setdefault('reported_at', None)
            for field in ('pr_keys', 'message_ids'):
                card[field] = json.loads(card[field])
            arc = con.execute('SELECT project_id FROM arcs WHERE arc_id=?', (card['arc_id'],)).fetchone() if 'arcs' in existing else None
            card['project_id'] = arc['project_id'] if arc else None
        for entry in result['arc_history']:
            entry['snapshot'] = json.loads(entry['snapshot'])
        for entry in result['promotion_batches']:
            self._promotion_result(entry)
