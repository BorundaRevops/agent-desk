"""Canonical PR identity and evidence-backed Desk lifecycle reconciliation."""
from __future__ import annotations

import json
import re


LIFECYCLE_CONTRACT = {
    'pr-close': 'repo, pr, owner_lane, expected_version, terminal_status (abandoned|superseded), reason, evidence:[nonempty references]. Closes only the exact current PR and deactivates its subscriptions',
    'lifecycle-reconcile': 'no fields. Canonicalizes known repository aliases, consolidates duplicate PR records, closes observed abandoned or explicitly superseded records, and completes responded requests only from exact PR or promotion evidence',
}

TERMINAL_PR_STATES = ('promoted', 'abandoned', 'superseded')
def canonical_repo(repo, url=None):
    """Use the caller's stable repository name; never infer it from a URL."""
    return str(repo).strip()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class LifecycleMixin:
    def _lifecycle_error(self, message):
        return self.lifecycle_error_type(message)

    def canonical_pr_payload(self, data):
        if not isinstance(data, dict) or 'repo' not in data:
            return data
        return dict(data, repo=canonical_repo(data['repo'], data.get('url')))

    def _lifecycle_evidence(self, value):
        if (not isinstance(value, list) or not value or len(value) > 20
                or any(not isinstance(item, str) or not item.strip() or len(item) > 4000 for item in value)):
            raise self._lifecycle_error('evidence must contain 1..20 nonempty references')
        return [item.strip() for item in value]

    def pr_close(self, con, data, now):
        if set(data) != {'repo', 'pr', 'owner_lane', 'expected_version', 'terminal_status', 'reason', 'evidence'}:
            raise self._lifecycle_error('pr-close requires exactly repo, pr, owner_lane, expected_version, terminal_status, reason and evidence')
        data = self.canonical_pr_payload(data)
        row = self.get_pr(con, data)
        self.pr_cas(row, data, 'owner_lane')
        terminal = data['terminal_status']
        if terminal not in ('abandoned', 'superseded'):
            raise self._lifecycle_error('terminal_status must be abandoned or superseded')
        reason = data['reason']
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000 or '\0' in reason:
            raise self._lifecycle_error('reason must be a nonempty string of at most 4000 characters')
        closure = dict(status=terminal, reason=reason.strip(), evidence=self._lifecycle_evidence(data['evidence']), closed_at=now)
        con.execute('UPDATE prs SET status=?,closure=?,version=version+1,updated_at=? WHERE repo=? AND pr=?',
                    (terminal, _json(closure), now, row['repo'], row['pr']))
        con.execute('UPDATE subscriptions SET active=0 WHERE topic=?', ('pr:' + row['repo'] + ':' + row['pr'],))
        return self.get_pr(con, data)

    def _canonical_pr_groups(self, con):
        groups = {}
        for raw in con.execute('SELECT * FROM prs'):
            row = dict(raw)
            key = (canonical_repo(row['repo'], row.get('url')), row['pr'])
            groups.setdefault(key, []).append(row)
        return groups

    def _merge_pr_group(self, con, canonical, pr, rows, now):
        ranks = {'promoted': 5, 'merged': 4, 'owned': 3, 'abandoned': 2, 'superseded': 1}
        ordered = sorted(rows, key=lambda row: (ranks.get(row['status'], 0), bool(row.get('attestation')), row['updated_at']), reverse=True)
        best = ordered[0]
        promoted = [row for row in rows if row['status'] == 'promoted']
        if len({(row.get('merge_commit'), row.get('attestation')) for row in promoted}) > 1:
            raise self._lifecycle_error('conflicting promoted PR aliases require manual review: ' + canonical + ' #' + pr)
        target = next((row for row in rows if row['repo'] == canonical), None)
        changed = len(rows) > 1 or target is None
        fields = {}
        for field in ('url', 'merge_commit', 'attestation', 'workstream_key', 'title', 'opened_by_lane', 'observed_status', 'closure'):
            fields[field] = next((row.get(field) for row in ordered if row.get(field) is not None), None)
        if target is not None and not changed:
            return False
        aliases = {row['repo'] for row in rows}
        topics = ['pr:' + row['repo'] + ':' + pr for row in rows]
        subscriptions = [dict(row) for row in con.execute(
            'SELECT * FROM subscriptions WHERE topic IN (' + ','.join('?' for _ in topics) + ')', topics)] if topics else []
        if target is None:
            con.execute('''INSERT INTO prs(repo,pr,owner_lane,version,status,url,merge_commit,attestation,updated_at,
                           workstream_key,title,opened_by_lane,observed_status,closure)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (canonical, pr, best['owner_lane'], max(row['version'] for row in rows) + 1, best['status'],
                         fields['url'], fields['merge_commit'], fields['attestation'], now, fields['workstream_key'],
                         fields['title'], fields['opened_by_lane'], fields['observed_status'], fields['closure']))
        else:
            con.execute('''UPDATE prs SET owner_lane=?,version=?,status=?,url=?,merge_commit=?,attestation=?,updated_at=?,
                           workstream_key=?,title=?,opened_by_lane=?,observed_status=?,closure=? WHERE repo=? AND pr=?''',
                        (best['owner_lane'], max(row['version'] for row in rows) + 1, best['status'], fields['url'],
                         fields['merge_commit'], fields['attestation'], now, fields['workstream_key'], fields['title'],
                         fields['opened_by_lane'], fields['observed_status'], fields['closure'], canonical, pr))
        for row in rows:
            if row['repo'] != canonical:
                con.execute('DELETE FROM prs WHERE repo=? AND pr=?', (row['repo'], pr))
        for item in subscriptions:
            active = 0 if best['status'] in TERMINAL_PR_STATES else item['active']
            con.execute('''INSERT INTO subscriptions(lane_id,topic,active,created_at) VALUES(?,?,?,?)
                           ON CONFLICT(lane_id,topic) DO UPDATE SET active=max(active,excluded.active)''',
                        (item['lane_id'], 'pr:' + canonical + ':' + pr, active, item['created_at']))
        if best['status'] in TERMINAL_PR_STATES:
            con.execute('UPDATE subscriptions SET active=0 WHERE topic=?', ('pr:' + canonical + ':' + pr,))
        con.execute('DELETE FROM subscriptions WHERE topic IN (' + ','.join('?' for _ in topics) + ') AND topic!=?', (*topics, 'pr:' + canonical + ':' + pr))
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cards'").fetchone():
            for card in con.execute('SELECT workstream_key,pr_keys FROM cards').fetchall():
                keys = json.loads(card['pr_keys'])
                normalized = []
                for key in keys:
                    repo = canonical_repo(key['repo']) if str(key['pr']) == pr and key['repo'] in aliases else key['repo']
                    item = {'repo': repo, 'pr': str(key['pr'])}
                    if item not in normalized:
                        normalized.append(item)
                if normalized != keys:
                    con.execute('UPDATE cards SET pr_keys=?,version=version+1,updated_at=? WHERE workstream_key=?',
                                (_json(normalized), now, card['workstream_key']))
        for request in con.execute('SELECT request_id,resource,completion_rule FROM requests').fetchall():
            updates = {}
            for field in ('resource', 'completion_rule'):
                value = json.loads(request[field]) if request[field] else None
                if isinstance(value, dict) and str(value.get('pr')) == pr and value.get('repo') in aliases:
                    value['repo'] = canonical
                    updates[field] = _json(value)
            if updates:
                con.execute('UPDATE requests SET ' + ','.join(key + '=?' for key in updates) + ',updated_at=? WHERE request_id=?',
                            (*updates.values(), now, request['request_id']))
        return True

    def _request_pr_candidate(self, con, request):
        resource = json.loads(request['resource']) if request['resource'] else None
        rows = []
        if isinstance(resource, dict) and resource.get('repo') and resource.get('pr'):
            rows = con.execute('SELECT * FROM prs WHERE repo=? AND pr=?',
                               (canonical_repo(resource['repo'], resource.get('url')), str(resource['pr']))).fetchall()
        if not rows:
            matches = re.findall(r'\bPR\s*#?\s*([1-9][0-9]*)\b', request['title'], re.I)
            if len(set(matches)) == 1:
                candidates = [row for row in con.execute('SELECT * FROM prs WHERE pr=?', (matches[0],))
                              if row['owner_lane'] == request['owner_lane'] or row['workstream_key'] == request['workstream_key']]
                if len(candidates) == 1:
                    rows = candidates
        return dict(rows[0]) if len(rows) == 1 else None

    def _promotion_for_request(self, con, request):
        resource = json.loads(request['resource']) if request['resource'] else None
        source = ' '.join(filter(None, [request['title'], request['body'], resource.get('url') if isinstance(resource, dict) else None,
                                       str(resource.get('build')) if isinstance(resource, dict) and resource.get('build') else None]))
        builds = set(re.findall(r'\b(?:build\s*|buildId=)([1-9][0-9]*)\b', source, re.I))
        if not builds:
            return None
        found = []
        for batch in con.execute('SELECT * FROM promotion_batches'):
            proof = ' '.join([batch['attestation'], batch['evidence'], batch['image'], batch['target']])
            if any(re.search(r'\b(?:build\s*|imageTag\s*)' + re.escape(build) + r'\b', proof, re.I) for build in builds):
                found.append(dict(batch))
        return found[0] if len(found) == 1 else None

    def _complete_request(self, con, request, evidence, now):
        projected = self._request_projection(con, request, now)
        if projected['state'] != 'responded' or not projected['response'] or projected['response']['choice'] == 'later':
            return False
        rule = json.loads(request['completion_rule'])
        if rule['type'] != 'human_response':
            return False
        verification = dict(type='human_response', status='completed', checked_at=now, recorded_at=now,
                            source='lifecycle_reconciler', owner_lane=request['owner_lane'], evidence=evidence)
        con.execute("UPDATE requests SET verification=?,definition_state='completed',updated_at=? WHERE request_id=?",
                    (_json(verification), now, request['request_id']))
        self._request_audit(con, 'request-verified', dict(request_id=request['request_id'], verification=verification), now)
        self._request_persist(con, request['request_id'], now)
        return True

    def _lifecycle_reconcile_requests(self, con, now):
        completed = []
        for raw in con.execute("SELECT * FROM requests WHERE state='responded' AND definition_state NOT IN ('completed','cancelled')"):
            request = dict(raw)
            pr = self._request_pr_candidate(con, request)
            evidence = None
            if pr:
                promotion = 'promot' in request['title'].casefold()
                complete = pr['status'] == 'promoted' if promotion else pr['status'] in ('merged', 'promoted')
                if complete:
                    evidence = ['Desk PR registry: %s PR %s version %s, status %s' %
                                (pr['repo'], pr['pr'], pr['version'], pr['status'])]
                    if pr.get('url'):
                        evidence.append(pr['url'])
                    if pr.get('attestation'):
                        evidence.append('Verified promotion attestation is recorded on the PR')
            if evidence is None and 'promot' in request['title'].casefold():
                batch = self._promotion_for_request(con, request)
                if batch:
                    evidence = ['Desk promotion batch: ' + batch['batch_id'], 'Deployed commit: ' + batch['deployed_commit']]
            if evidence and self._complete_request(con, request, evidence, now):
                completed.append(request['request_id'])
        projected = self.conversation_projection(con, now)
        for row in con.execute('SELECT request_id FROM requests').fetchall():
            self._request_reconcile_one(con, row['request_id'], now, projected)
        return completed

    def lifecycle_reconcile(self, con, data, now):
        if data:
            raise self._lifecycle_error('lifecycle-reconcile accepts no fields')
        canonicalized = []
        for (repo, pr), rows in list(self._canonical_pr_groups(con).items()):
            if self._merge_pr_group(con, repo, pr, rows, now):
                canonicalized.append({'repo': repo, 'pr': pr, 'aliases': sorted({row['repo'] for row in rows})})
        closed = []
        for raw in con.execute("SELECT * FROM prs WHERE status NOT IN ('promoted','abandoned','superseded')"):
            row = dict(raw)
            observed = json.loads(row['observed_status']) if row.get('observed_status') else None
            title = (row.get('title') or '').casefold()
            terminal = 'abandoned' if observed and observed.get('status') == 'abandoned' else ('superseded' if 'phantom' in title or 'superseded duplicate' in title else None)
            if terminal:
                closure = dict(status=terminal, reason='Reconciled from saved provider observation or explicit superseded marker',
                               evidence=['Desk observed status: ' + (observed.get('checked_at') if observed else row.get('title') or '')], closed_at=now)
                con.execute('UPDATE prs SET status=?,closure=?,version=version+1,updated_at=? WHERE repo=? AND pr=?',
                            (terminal, _json(closure), now, row['repo'], row['pr']))
                con.execute('UPDATE subscriptions SET active=0 WHERE topic=?', ('pr:' + row['repo'] + ':' + row['pr'],))
                closed.append({'repo': row['repo'], 'pr': row['pr'], 'status': terminal})
        requests_completed = self._lifecycle_reconcile_requests(con, now)
        return {'canonicalized': canonicalized, 'closed': closed, 'requests_completed': requests_completed}
