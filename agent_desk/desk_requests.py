"""First-class Desk obligations and immutable human response events.

All mutations run inside Desk's existing BEGIN IMMEDIATE transaction. These
commands record trusted local assertions; authentication belongs to the caller.
They neither contact a provider nor execute the action being discussed.
"""
import hashlib
import json
import math
import re
import uuid
from datetime import datetime, timezone


REQUEST_CONTRACT = {
    'request-open': 'source_key, workstream_key, owner_lane (registered nonhuman), kind (approval|merge|input|review|manual), title, body (new definitions require exactly four nonempty labeled paragraphs: Context:, Why it matters:, What I need from you:, What happens next:; labels are case-insensitive and may be bold); optional source_message_id, resource object (response_prompt: {question, options:2..6 [{label, choice:kind-specific, body:reply text, require_reason:boolean}]}), completion_rule ({type:human_response} or {type:pr_merged,repo,pr}). Definition/version 1 is immutable; reuse preserves existing definition and reports definition_conflict',
    'request-respond': 'request_id, id (idempotent UUID), lane_id (human supplied by trusted caller), expected_version, choice (kind-specific approve|decline|reply|completed|later), body (0..4000), expected_response_ids (all known event UUIDs); optional trusted created_at (seconds or ISO UTC). Response and owner message are atomic; concurrent incomparable events conflict',
    'request-import': 'request_id, lane_id (trusted human), responses (canonical request-respond events). Saves a complete causal batch atomically and notifies its final reducer outcome once; no intermediate approval notification',
    'request-verify': 'request_id, owner_lane, expected_version, verification {type (matching completion_rule), status (pr_merged:active|completed|abandoned; human_response:pending|completed), checked_at (seconds or ISO UTC), evidence:[nonempty references]; pr_merged also requires exact repo,pr. Records read-only evidence, never acts on external systems',
    'request-cancel': 'request_id, owner_lane (exact registered nonhuman owner), expected_version, reason (nonempty, 1..4000). Cancels the obligation without changing response or verification proof. Repeats preserve the first cancellation; verified completion is already terminal',
    'request-reconcile': 'optional threads (complete trusted external work-board rows; explicit action or action_request_id UUID, exact workstreams bindings required). An action_request_id must identify an existing request with that exact owner/workstream and skips compatibility inference. Explicitly linked requests require owner cancellation even if the board action clears. Reconciles full message/PR ledger and imports legacy replies/done without reading. Omit threads to reconcile ledger only',
    'request-list': 'optional owner_lane (registered lane), request_history_limit (0..1000, default 100). Read-only: every nonterminal request first, independently bounded terminal history',
}

REQUEST_SCHEMA = '''
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, source_key TEXT NOT NULL UNIQUE, version INTEGER NOT NULL,
 workstream_key TEXT NOT NULL, owner_lane TEXT NOT NULL REFERENCES lanes(lane_id),
 source_message_id TEXT REFERENCES messages(message_id), kind TEXT NOT NULL,
 title TEXT NOT NULL, body TEXT NOT NULL, resource TEXT, completion_rule TEXT NOT NULL,
 state TEXT NOT NULL, response TEXT, verification TEXT,
 created_at REAL NOT NULL, updated_at REAL NOT NULL,
 definition_state TEXT NOT NULL DEFAULT 'open', compatibility INTEGER NOT NULL DEFAULT 0,
 cancellation TEXT, owner_managed INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_requests_owner ON requests(owner_lane,state,updated_at);
CREATE INDEX IF NOT EXISTS ix_requests_workstream ON requests(workstream_key,source_key);
CREATE TABLE IF NOT EXISTS request_responses (
 id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(request_id),
 lane_id TEXT NOT NULL REFERENCES lanes(lane_id), expected_version INTEGER NOT NULL,
 choice TEXT NOT NULL, body TEXT NOT NULL, expected_response_ids TEXT NOT NULL,
 created_at REAL NOT NULL, notification_message_id TEXT REFERENCES messages(message_id),
 origin TEXT NOT NULL DEFAULT 'human');
CREATE INDEX IF NOT EXISTS ix_request_responses_request ON request_responses(request_id,created_at,id);
'''

KINDS = ('approval', 'merge', 'input', 'review', 'manual')
CHOICES = {
    'approval': ('approve', 'decline', 'reply', 'completed', 'later'),
    'merge': ('completed', 'reply', 'later'),
    'input': ('reply', 'later'),
    'review': ('completed', 'reply', 'later'),
    'manual': ('completed', 'reply', 'later'),
}
TERMINAL = ('completed', 'cancelled')
DEFINITION_FIELDS = ('source_key', 'version', 'workstream_key', 'owner_lane',
                     'source_message_id', 'kind', 'title', 'body', 'resource', 'completion_rule')
REQUEST_BODY_LABELS = ('Context', 'Why it matters', 'What I need from you', 'What happens next')
_BODY_LABEL_ALTERNATIVES = '|'.join(re.escape(label) for label in REQUEST_BODY_LABELS)
REQUEST_BODY_LABEL = re.compile(
    r'^[ \t]*(?:\*\*(?P<bold>' + _BODY_LABEL_ALTERNATIVES + r')(?:\*\*:|:\*\*)|'
    r'(?P<plain>' + _BODY_LABEL_ALTERNATIVES + r'):)[ \t]*', re.I | re.M)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


class RequestMixin:
    # Desk supplies its public DeskError class without a circular import.
    def _request_error(self, message):
        return self.request_error_type(message)

    def _request_text(self, data, name, maximum, empty=False):
        value = data.get(name)
        if not isinstance(value, str) or len(value) > maximum or '\0' in value or (not empty and not value.strip()):
            raise self._request_error('%s must be a %sstring of %s..%s characters' %
                                      (name, '' if empty else 'nonempty ', 0 if empty else 1, maximum))
        return value

    def _request_validate_body(self, body):
        # Validate structure only; section text is the owner's assertion, not
        # evidence from which Desk may infer a request kind or authority.
        matches = list(REQUEST_BODY_LABEL.finditer(body))
        labels = [(match['bold'] or match['plain']).casefold() for match in matches]
        required = {label.casefold() for label in REQUEST_BODY_LABELS}
        nonempty = all(body[match.end():matches[index + 1].start() if index + 1 < len(matches)
                            else len(body)].strip() for index, match in enumerate(matches))
        if (len(matches) != len(REQUEST_BODY_LABELS) or set(labels) != required or not nonempty
                or body[:matches[0].start()].strip()):
            raise self._request_error('body must have exactly four nonempty labeled paragraphs, each once: ' +
                                      ', '.join(label + ':' for label in REQUEST_BODY_LABELS))

    def _request_uuid(self, value, name):
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise self._request_error(name + ' must be a canonical UUID')
        return value

    def _request_stamp(self, value, name):
        if isinstance(value, str):
            try:
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)', value):
                    raise ValueError()
                stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if stamp.utcoffset() != timezone.utc.utcoffset(stamp):
                    raise ValueError()
                value = stamp.timestamp()
            except (ValueError, TypeError, OverflowError):
                raise self._request_error(name + ' must be seconds or an ISO UTC timestamp')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise self._request_error(name + ' must be finite nonnegative seconds or an ISO UTC timestamp')
        return float(value)

    def _request_key(self, data):
        key = data.get('workstream_key')
        if not isinstance(key, str) or len(key) > 200 or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', key):
            raise self._request_error('workstream_key must be a lowercase hyphenated key of 1..200 characters')
        return key

    def _request_owner(self, con, owner_lane):
        if not isinstance(owner_lane, str):
            raise self._request_error('owner_lane must be a registered nonhuman lane')
        lane = self.lane(con, owner_lane)
        if lane['provider'].casefold() == 'human':
            raise self._request_error('owner_lane must be a registered nonhuman lane')
        return lane

    def _request_expected_version(self, data, row):
        expected = data.get('expected_version')
        if isinstance(expected, bool) or not isinstance(expected, int) or expected != row['version']:
            raise self._request_error('stale request definition version')

    def _request_definition(self, con, data, now):
        source_key = self._request_text(data, 'source_key', 300)
        key = self._request_key(data)
        owner = self._request_owner(con, data.get('owner_lane'))['lane_id']
        binding = con.execute('SELECT lane_id FROM workstreams WHERE workstream_key=?', (key,)).fetchone()
        if binding and binding['lane_id'] != owner:
            raise self._request_error('request owner does not match the exact workstream binding')
        kind = data.get('kind')
        if kind not in KINDS:
            raise self._request_error('unsupported request kind')
        title = self._request_text(data, 'title', 500)
        body = self._request_text(data, 'body', 4000, empty=True)
        resource = data.get('resource')
        if resource is not None and not isinstance(resource, dict):
            raise self._request_error('resource must be an object or null')
        prompt = (resource or {}).get('response_prompt')
        if prompt is not None:
            if not isinstance(prompt, dict) or set(prompt) != {'question', 'options'}:
                raise self._request_error('response_prompt requires question and options')
            self._request_text(prompt, 'question', 500)
            options = prompt['options']
            if not isinstance(options, list) or not 2 <= len(options) <= 6:
                raise self._request_error('response_prompt requires 2..6 options')
            labels = set()
            for option in options:
                if not isinstance(option, dict) or set(option) - {'label', 'choice', 'body', 'require_reason'}:
                    raise self._request_error('invalid response_prompt option')
                label = self._request_text(option, 'label', 80)
                if label in labels or option.get('choice') not in CHOICES[kind]:
                    raise self._request_error('response_prompt labels must be unique and choices allowed for this kind')
                labels.add(label)
                if 'require_reason' in option and not isinstance(option['require_reason'], bool):
                    raise self._request_error('require_reason must be boolean')
                if 'body' in option:
                    self._request_text(option, 'body', 1000, empty=True)
                if option['choice'] == 'reply' and not option.get('body', '').strip():
                    raise self._request_error('reply options require a nonempty body')
        rule = data.get('completion_rule', {'type': 'human_response'})
        if not isinstance(rule, dict) or rule.get('type') not in ('human_response', 'pr_merged'):
            raise self._request_error('completion_rule requires human_response or pr_merged')
        if rule['type'] == 'human_response':
            if set(rule) != {'type'}:
                raise self._request_error('human_response completion_rule must contain only type')
            rule = {'type': 'human_response'}
        else:
            if kind != 'merge' or set(rule) != {'type', 'repo', 'pr'}:
                raise self._request_error('pr_merged completion_rule requires merge kind and exact repo,pr')
            repo = self._request_text(rule, 'repo', 1000)
            pr = str(rule.get('pr', ''))
            if isinstance(rule.get('pr'), bool) or not re.fullmatch(r'[1-9][0-9]*', pr):
                raise self._request_error('completion_rule.pr must be a positive PR number')
            rule = {'type': 'pr_merged', 'repo': repo, 'pr': pr}
            if resource is not None and any(str(resource.get(k, rule[k])) != rule[k] for k in ('repo', 'pr')):
                raise self._request_error('resource PR identity does not match completion_rule')
        try:
            canonical(resource)
            canonical(rule)
        except (ValueError, TypeError):
            raise self._request_error('resource and completion_rule must contain finite JSON values')
        source_id = data.get('source_message_id')
        if source_id is not None:
            self._request_uuid(source_id, 'source_message_id')
            source = con.execute('SELECT * FROM messages WHERE message_id=?', (source_id,)).fetchone()
            if not source:
                raise self._request_error('source message is absent; cannot confirm request')
            if source['from_lane'] != owner or self.lane(con, source['to_lane'])['provider'] != 'human':
                raise self._request_error('source message must be from the exact owner to the human recipient')
            projected = self.conversation_projection(con, now)[source_id]
            if projected['workstream_key'] not in (None, key):
                raise self._request_error('source message belongs to another workstream')
        return dict(source_key=source_key, version=1, workstream_key=key, owner_lane=owner,
                    source_message_id=source_id, kind=kind, title=title, body=body,
                    resource=resource, completion_rule=rule)

    def _request_record(self, con, request_id):
        self._request_uuid(request_id, 'request_id')
        row = con.execute('SELECT * FROM requests WHERE request_id=?', (request_id,)).fetchone()
        if not row:
            raise self._request_error('request is absent')
        return dict(row)

    def _request_events(self, con, request_id, typed=False):
        events = []
        where = " AND origin!='legacy'" if typed else ''
        for raw in con.execute('SELECT * FROM request_responses WHERE request_id=?' + where + ' ORDER BY created_at,id', (request_id,)):
            event = dict(raw)
            event['expected_response_ids'] = json.loads(event['expected_response_ids'])
            events.append(event)
        return events

    def _request_projection(self, con, row, now):
        result = dict(row)
        for field in ('resource', 'completion_rule', 'response', 'verification', 'cancellation'):
            result[field] = json.loads(result[field]) if result.get(field) else None
        all_events = self._request_events(con, row['request_id'])
        events = [event for event in all_events if event['origin'] != 'legacy']
        legacy = next((event for event in all_events if event['origin'] == 'legacy'), None)
        referenced = {item for event in events for item in event['expected_response_ids']}
        heads = [event for event in events if event['id'] not in referenced]
        result['response_events'] = events
        result['response_ids'] = sorted(event['id'] for event in events)
        result['heads'] = heads
        result['legacy_response'] = legacy
        result['response'] = heads[0] if len(heads) == 1 else (legacy if not heads else None)
        result['deferred_until'] = None
        result['state'] = row['definition_state']
        if row['definition_state'] == 'cancelled':
            pass  # A withdrawn obligation stays closed; every response head remains proof.
        elif len(heads) > 1:
            result['state'] = 'conflict'
        elif row['definition_state'] in TERMINAL:
            pass
        elif heads:
            head = heads[0]
            if head['choice'] == 'later':
                result['deferred_until'] = head['created_at'] + 86400
                result['state'] = 'deferred' if result['deferred_until'] > now else 'open'
            elif head['choice'] == 'decline':
                result['state'] = 'completed'
            else:
                result['state'] = 'responded'
        result['choices'] = list(CHOICES[result['kind']])
        result['human_step_done'] = result['state'] in ('responded', 'completed', 'cancelled')
        result['owner_managed'] = bool(result.get('owner_managed') or not result['compatibility'])
        result['origin'] = 'board' if result.pop('compatibility') else 'explicit'
        return result

    def _request_audit(self, con, action, detail, now):
        con.execute('INSERT INTO audit(action,detail,created_at) VALUES(?,?,?)', (action, canonical(detail), now))

    def _request_persist(self, con, request_id, now):
        row = self._request_record(con, request_id)
        projected = self._request_projection(con, row, now)
        response = canonical(projected['response']) if projected['response'] is not None else None
        if row['state'] != projected['state'] or row['response'] != response:
            con.execute('UPDATE requests SET state=?,response=?,updated_at=? WHERE request_id=?',
                        (projected['state'], response, now, request_id))
            self._request_audit(con, 'request-state', dict(request_id=request_id,
                previous_state=row['state'], state=projected['state'], response_ids=projected['response_ids']), now)
            projected['updated_at'] = now
        return projected

    def _request_open(self, con, data, now, compatibility=False):
        definition = self._request_definition(con, data, now)
        existing = con.execute('SELECT * FROM requests WHERE source_key=?', (definition['source_key'],)).fetchone()
        if existing:
            result = self._request_projection(con, dict(existing), now)
            result['duplicate'] = True
            result['definition_conflict'] = any(result[k] != definition[k] for k in DEFINITION_FIELDS)
            return result
        if not compatibility:
            self._request_validate_body(definition['body'])
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'desk-request:' + definition['source_key']))
        con.execute('''INSERT INTO requests(request_id,source_key,version,workstream_key,owner_lane,source_message_id,
                       kind,title,body,resource,completion_rule,state,response,verification,created_at,updated_at,
                       definition_state,compatibility) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (request_id, definition['source_key'], 1, definition['workstream_key'], definition['owner_lane'],
                     definition['source_message_id'], definition['kind'], definition['title'], definition['body'],
                     canonical(definition['resource']) if definition['resource'] is not None else None,
                     canonical(definition['completion_rule']), 'open', None, None, now, now, 'open', int(compatibility)))
        self._request_audit(con, 'request-created', dict(request_id=request_id, source_key=definition['source_key'],
            workstream_key=definition['workstream_key'], owner_lane=definition['owner_lane'], kind=definition['kind']), now)
        self._request_reconcile_one(con, request_id, now)
        result = self._request_persist(con, request_id, now)
        return dict(result, duplicate=False, definition_conflict=False)

    def request_open(self, con, data, now):
        return self._request_open(con, data, now)

    def _request_response_text(self, request, event, conflict=False):
        closed = request['definition_state'] in TERMINAL
        intro = {
            'approve': 'The user explicitly approved this exact request scope.',
            'decline': 'The user explicitly declined this exact request. Do not treat it as approval.',
            'reply': 'The user replied to this exact request. Interpret the reply within existing authority.',
            'completed': 'The user reports: I already handled this request. This is a completion report, not verification or a new approval.',
            'later': 'The user deferred this request for 24 hours. No approval was granted.',
        }[event['choice']]
        if closed:
            intro = ('Historical response recorded. This request is already ' + request['definition_state'] + '. '
                     'This notification authorizes no new execution and does not reopen the request.\n'
                     'Recorded historical choice: ' + event['choice'] + '.')
            if conflict:
                intro += ' Conflicting historical responses remain preserved as evidence.'
        elif conflict:
            intro = ('Correction: conflicting human responses are recorded for this request. '
                     'Do not rely on a single response as current authorization. '
                     'Review the response heads and obtain a response superseding all known events.\n'
                     'The newly received choice is ' + event['choice'] + '.')
        body = intro + '\nRequest: ' + request['title'] + '\nScope:\n' + request['body']
        body += '\nRequest ID: ' + request['request_id'] + '\nWorkstream: ' + request['workstream_key']
        if request.get('resource') and request['resource'].get('url'):
            body += '\nResource: ' + str(request['resource']['url'])
        if event['body']:
            body += '\nUser response:\n' + event['body']
        if conflict:
            body += '\nConflicting response heads:\n' + '\n'.join(
                '- ' + head['id'] + ': ' + head['choice'] + ('; ' + head['body'] if head['body'] else '')
                for head in request['heads'])
            body += '\nAll known response IDs: ' + ', '.join(request['response_ids'])
        elif event['expected_response_ids']:
            body += '\nThis response supersedes these known response events: ' + ', '.join(event['expected_response_ids'])
        return body

    def _request_notify(self, con, request, event, now):
        conflict = len(request['heads']) > 1
        closed = request['definition_state'] in TERMINAL
        dedup = ('request-conflict:' + request['request_id'] + ':' + hashlib.sha256(
            canonical(sorted(head['id'] for head in request['heads'])).encode()).hexdigest()) if conflict else 'request-response:' + event['id']
        notice = dict(from_lane=event['lane_id'], to_lane=request['owner_lane'], kind='update' if closed else 'work_request',
                      dedup_key=dedup,
                      body=self._request_response_text(request, event, conflict),
                      workstream_key=request['workstream_key'])
        if not conflict and request.get('source_message_id'):
            source = self.conversation_projection(con, now).get(request['source_message_id'])
            if (source and source['current'] and source['state'] != 'resolved'
                    and source['from_lane'] == request['owner_lane'] and source['to_lane'] == event['lane_id']
                    and source['workstream_key'] == request['workstream_key']):
                notice['parent_id'] = source['message_id']
        return self.message_send(con, notice, now)

    def _request_store_response(self, con, data, now):
        allowed = {'request_id', 'id', 'lane_id', 'expected_version', 'choice', 'body', 'expected_response_ids', 'created_at'}
        if set(data) - allowed:
            raise self._request_error('unsupported request response fields: ' + ', '.join(sorted(set(data) - allowed)))
        row = self._request_record(con, data.get('request_id'))
        self._request_expected_version(data, row)
        event_id = self._request_uuid(data.get('id'), 'id')
        lane_id = data.get('lane_id')
        if not isinstance(lane_id, str) or self.lane(con, lane_id)['provider'] != 'human':
            raise self._request_error('request response requires a human lane from a trusted caller')
        choice = data.get('choice')
        if choice not in CHOICES[row['kind']]:
            raise self._request_error('choice is not allowed for this request kind')
        body = self._request_text(data, 'body', 4000, empty=True)
        if choice == 'reply' and not body.strip():
            raise self._request_error('reply requires a nonempty body')
        known = data.get('expected_response_ids')
        if not isinstance(known, list) or len(known) > 10000:
            raise self._request_error('expected_response_ids must be a list of at most 10000 UUIDs')
        known = [self._request_uuid(item, 'expected_response_ids item') for item in known]
        if len(known) != len(set(known)) or event_id in known:
            raise self._request_error('expected_response_ids must be unique and cannot reference the current event')
        known = sorted(known)
        prior = con.execute('SELECT * FROM request_responses WHERE id=?', (event_id,)).fetchone()
        created_at = self._request_stamp(data.get('created_at', prior['created_at'] if prior else now), 'created_at')
        event = dict(id=event_id, request_id=row['request_id'], lane_id=lane_id, expected_version=row['version'],
                     choice=choice, body=body, expected_response_ids=known, created_at=created_at)
        if prior:
            persisted = dict(prior)
            persisted['expected_response_ids'] = json.loads(persisted['expected_response_ids'])
            if persisted['origin'] == 'legacy' or any(event[k] != persisted[k] for k in event if k != 'created_at'):
                raise self._request_error('response id already used with different content')
            # Cloud retry records may carry different server receipt times. Their
            # canonical event uses the earliest one, which can never extend Later.
            if event['created_at'] < persisted['created_at']:
                con.execute('UPDATE request_responses SET created_at=? WHERE id=?', (event['created_at'], event_id))
            event['created_at'] = min(event['created_at'], persisted['created_at'])
            return event, True
        events = {item['id']: item for item in self._request_events(con, row['request_id'], typed=True)}
        if any(item not in events for item in known):
            raise self._request_error('expected_response_ids contains an absent or different request event')
        if any(not set(events[item]['expected_response_ids']).issubset(known) for item in known):
            raise self._request_error('expected_response_ids must include all prior known causal events')
        con.execute('''INSERT INTO request_responses(id,request_id,lane_id,expected_version,choice,body,
                       expected_response_ids,created_at,notification_message_id,origin) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (event_id, row['request_id'], lane_id, row['version'], choice, body, canonical(known), created_at, None, 'human'))
        return event, False

    def request_respond(self, con, data, now):
        return self.request_import(con, {'request_id': data.get('request_id'),
                                        'lane_id': data.get('lane_id'), 'responses': [data]}, now)

    def request_import(self, con, data, now):
        if set(data) - {'request_id', 'lane_id', 'responses'}:
            raise self._request_error('unsupported request import fields')
        row = self._request_record(con, data.get('request_id'))
        lane_id = data.get('lane_id')
        if not isinstance(lane_id, str) or self.lane(con, lane_id)['provider'] != 'human':
            raise self._request_error('request import requires a human lane from a trusted caller')
        responses = data.get('responses')
        if not isinstance(responses, list) or not responses or len(responses) > 10000:
            raise self._request_error('responses must contain 1..10000 canonical response events')
        pending = []
        for response in responses:
            if not isinstance(response, dict):
                raise self._request_error('response events must be objects')
            if response.get('request_id', row['request_id']) != row['request_id'] or response.get('lane_id', lane_id) != lane_id:
                raise self._request_error('response event actor or request differs from trusted import scope')
            event = dict(response, request_id=row['request_id'], lane_id=lane_id)
            self._request_uuid(event.get('id'), 'id')
            if not isinstance(event.get('expected_response_ids'), list):
                raise self._request_error('expected_response_ids must be a list')
            for item in event['expected_response_ids']:
                self._request_uuid(item, 'expected_response_ids item')
            pending.append(event)
        imported = []
        while pending:
            known = {event['id'] for event in self._request_events(con, row['request_id'], typed=True)}
            ready = [event for event in pending if set(event['expected_response_ids']).issubset(known)]
            if not ready:
                raise self._request_error('response batch contains absent, cyclic or different request event references')
            for event in ready:
                saved, duplicate = self._request_store_response(con, event, now)
                if not duplicate:
                    imported.append(saved['id'])
                pending.remove(event)
        request = self._request_projection(con, row, now)
        notification_id = None
        if imported:
            # Notification is an outcome of the whole batch, never a timestamp winner.
            representative = next((event for event in request['heads'] if event['id'] in imported), request['heads'][0])
            notification = self._request_notify(con, request, representative, now)
            notification_id = notification['message_id']
            con.execute('UPDATE request_responses SET notification_message_id=? WHERE id=?',
                        (notification_id, representative['id']))
        else:
            notification_id = next((event['notification_message_id'] for event in request['heads']
                                    if event['notification_message_id']), None)
        result = self._request_persist(con, row['request_id'], now)
        return dict(result, duplicate=not imported, imported_response_ids=imported,
                    notification_message_id=notification_id)

    def request_verify(self, con, data, now):
        row = self._request_record(con, data.get('request_id'))
        self._request_expected_version(data, row)
        owner = self._request_owner(con, data.get('owner_lane'))['lane_id']
        if owner != row['owner_lane']:
            raise self._request_error('verification requires the exact request owner')
        verification = data.get('verification')
        rule = json.loads(row['completion_rule'])
        if not isinstance(verification, dict) or verification.get('type') != rule['type']:
            raise self._request_error('verification type must match completion_rule')
        evidence = verification.get('evidence')
        if (not isinstance(evidence, list) or not evidence or len(evidence) > 20
                or any(not isinstance(x, str) or not x.strip() or len(x) > 4000 for x in evidence)):
            raise self._request_error('verification evidence must contain 1..20 nonempty references')
        stamp = self._request_stamp(verification.get('checked_at'), 'verification.checked_at')
        status = verification.get('status')
        allowed = {'type', 'status', 'checked_at', 'evidence', 'source'}
        if rule['type'] == 'pr_merged':
            allowed |= {'repo', 'pr'}
            if status not in ('active', 'completed', 'abandoned'):
                raise self._request_error('PR verification requires active, completed or abandoned status')
            if verification.get('repo') != rule['repo'] or str(verification.get('pr')) != rule['pr']:
                raise self._request_error('verification PR identity does not match completion_rule')
        else:
            if status not in ('pending', 'completed'):
                raise self._request_error('human_response verification requires pending or completed status')
            projection = self._request_projection(con, row, now)
            if status == 'completed' and (not projection['response'] or projection['response']['choice'] == 'later'):
                raise self._request_error('human_response completion requires a recorded human response resolving the request')
        if set(verification) - allowed:
            raise self._request_error('unsupported verification fields')
        if 'source' in verification and (not isinstance(verification['source'], str) or not verification['source'].strip()):
            raise self._request_error('verification source must be a nonempty string')
        verification = dict(verification, checked_at=stamp, owner_lane=owner, recorded_at=now)
        if rule['type'] == 'pr_merged':
            verification['pr'] = rule['pr']
        previous = json.loads(row['verification']) if row['verification'] else None
        if (previous and (previous.get('status') == 'completed'
                          or previous.get('checked_at', 0) > stamp)) or row['definition_state'] == 'cancelled':
            return dict(self._request_projection(con, row, now), preserved_verification=True)
        state = 'completed' if status == 'completed' else ('cancelled' if status == 'abandoned' else row['definition_state'])
        con.execute('UPDATE requests SET verification=?,definition_state=?,updated_at=? WHERE request_id=?',
                    (canonical(verification), state, now, row['request_id']))
        return self._request_persist(con, row['request_id'], now)

    def request_cancel(self, con, data, now):
        if set(data) - {'request_id', 'owner_lane', 'expected_version', 'reason'}:
            raise self._request_error('unsupported request cancellation fields')
        row = self._request_record(con, data.get('request_id'))
        self._request_expected_version(data, row)
        owner = self._request_owner(con, data.get('owner_lane'))['lane_id']
        if owner != row['owner_lane']:
            raise self._request_error('cancellation requires the exact request owner')
        reason = self._request_text(data, 'reason', 4000)
        if row['definition_state'] in TERMINAL:
            return dict(self._request_projection(con, row, now), duplicate=True, preserved_terminal=True)
        cancellation = dict(owner_lane=owner, reason=reason, cancelled_at=now)
        con.execute("UPDATE requests SET definition_state='cancelled',cancellation=?,updated_at=? WHERE request_id=?",
                    (canonical(cancellation), now, row['request_id']))
        self._request_audit(con, 'request-cancelled', dict(request_id=row['request_id'], cancellation=cancellation), now)
        return dict(self._request_persist(con, row['request_id'], now), duplicate=False)

    def _request_import_legacy(self, con, request, projected, now):
        source_id = request['source_message_id']
        if not source_id or source_id not in projected or self._request_events(con, request['request_id']):
            return
        source = projected[source_id]
        human = self.lane(con, source['to_lane'])
        if source['from_lane'] != request['owner_lane'] or human['provider'] != 'human':
            return
        child = next((item for item in projected.values() if item['parent_id'] == source_id), None)
        if child and child['from_lane'] == human['lane_id'] and child['to_lane'] == request['owner_lane']:
            identity, choice, body, stamp, message_id = ('reply:' + child['message_id'], 'reply',
                child['body'][:4000], child['created_at'], child['message_id'])
        elif source['state'] == 'resolved' and not child:
            identity, choice, body, stamp, message_id = ('done:' + source_id, 'completed',
                str(source.get('resolution') or '')[:4000], source.get('resolved_at') or now, None)
        else:
            return
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'desk-request-legacy:' + request['request_id'] + ':' + identity))
        con.execute('''INSERT OR IGNORE INTO request_responses(id,request_id,lane_id,expected_version,choice,body,
                       expected_response_ids,created_at,notification_message_id,origin) VALUES(?,?,?,?,?,?,?,?,?,?)''',
                    (event_id, request['request_id'], human['lane_id'], 1, choice, body, '[]', stamp, message_id, 'legacy'))
        if request['definition_state'] not in TERMINAL:
            con.execute("UPDATE requests SET definition_state='responded' WHERE request_id=?", (request['request_id'],))
        self._request_audit(con, 'request-legacy-response', dict(request_id=request['request_id'], id=event_id,
            source_message_id=source_id, response_message_id=message_id, choice=choice), now)

    def _request_reconcile_one(self, con, request_id, now, projected=None):
        request = self._request_record(con, request_id)
        projected = self.conversation_projection(con, now) if projected is None else projected
        self._request_import_legacy(con, request, projected, now)
        rule = json.loads(request['completion_rule'])
        if rule['type'] == 'pr_merged' and request['definition_state'] not in TERMINAL:
            pr = con.execute('SELECT * FROM prs WHERE repo=? AND pr=?', (rule['repo'], rule['pr'])).fetchone()
            if pr:
                observed = json.loads(pr['observed_status']) if pr['observed_status'] else None
                if pr['status'] in ('merged', 'promoted') or (observed and observed.get('status') == 'completed'):
                    stamp = self._request_stamp(observed['checked_at'], 'PR checked_at') if observed and observed.get('status') == 'completed' else pr['updated_at']
                    evidence = ['Desk PR registry: %s PR %s version %s, status %s' %
                                (pr['repo'], pr['pr'], pr['version'], pr['status'])]
                    if pr['url']:
                        evidence.append(pr['url'])
                    if pr['merge_commit']:
                        evidence.append('Merge commit: ' + pr['merge_commit'])
                    verification = dict(type='pr_merged', repo=pr['repo'], pr=pr['pr'], status='completed',
                                        checked_at=stamp, recorded_at=now, source='pr_registry', evidence=evidence)
                    con.execute('UPDATE requests SET verification=?,definition_state=?,updated_at=? WHERE request_id=?',
                                (canonical(verification), 'completed', now, request_id))
                    self._request_audit(con, 'request-verified', dict(request_id=request_id, verification=verification), now)
        return self._request_persist(con, request_id, now)

    def _request_compat_body(self, thread, action):
        normalized = ' '.join(action.casefold().split())
        context = []
        for field, label in (('note', 'Workstream update'), ('prerequisite', 'Current prerequisite'),
                             ('goal', 'Workstream goal'), ('next_step', 'Agent next step')):
            value = thread.get(field)
            if (isinstance(value, str) and value.strip() and '\0' not in value
                    and ' '.join(value.casefold().split()) != normalized):
                context.append(label + ': ' + value.strip())
        if not context:
            return action
        body = '\n\n'.join(context + ['Requested action: ' + action])
        # Context must never truncate the original action or make an otherwise
        # supported compatibility request fail the existing body-size contract.
        return body if len(body) <= 4000 else action

    def _request_compat_definition(self, con, thread, now):
        key = self._request_key({'workstream_key': thread.get('workstream_key', thread.get('key'))})
        action = thread.get('action')
        if thread.get('status') not in ('waiting', 'stalled') or not isinstance(action, str) or not action.strip():
            return None
        binding = con.execute('SELECT lane_id FROM workstreams WHERE workstream_key=?', (key,)).fetchone()
        if not binding:
            raise self._request_error('workstream has no exact registered owner binding')
        owner = binding['lane_id']
        if thread.get('owner_lane', owner) != owner:
            raise self._request_error('Board owner_lane does not match the exact workstream binding')
        self._request_owner(con, owner)
        source_id = thread.get('action_message_id') or None
        if source_id is not None:
            self._request_uuid(source_id, 'action_message_id')
        action = action.strip()
        normalized = ' '.join(action.casefold().split())
        source_key = 'message:' + source_id if source_id else 'board:' + key + ':' + normalized
        if len(source_key) > 300:
            source_key = 'board:' + key + ':action:' + hashlib.sha256(normalized.encode()).hexdigest()
        kind = 'manual'
        rule = {'type': 'human_response'}
        resource = None
        # Deliberately narrow: a compound promotion/approval request is not a merge obligation.
        merge = re.fullmatch(r'(?:please\s+)?merge\s+pr\s*#?\s*([1-9][0-9]*)(?:\s*\((?:AB#\d+(?:\s+slice\s+\d+)?|slice\s+\d+)\))?\s*[.!]?', action, re.I)
        if merge:
            rows = [dict(row) for row in con.execute('SELECT * FROM prs WHERE pr=? AND workstream_key=?', (merge.group(1), key))]
            link = thread.get('link') or thread.get('url')
            if link:
                rows = [row for row in rows if row.get('url') == link]
            if len(rows) == 1 and rows[0]['owner_lane'] == owner:
                pr = rows[0]
                kind = 'merge'
                rule = dict(type='pr_merged', repo=pr['repo'], pr=pr['pr'])
                resource = dict(type='pull_request', repo=pr['repo'], pr=pr['pr'], url=pr.get('url'))
                source_key = 'board:' + key + ':merge:' + pr['repo'] + ':' + pr['pr']
                if len(source_key) > 300:
                    source_key = 'board:' + key + ':merge:' + hashlib.sha256(canonical([pr['repo'], pr['pr']]).encode()).hexdigest()
        elif re.match(r'^(?:please\s+)?(?:approve|authorize|(?:give|provide)\s+(?:the\s+)?(?:(?:promote|promotion)\s+)?go(?:-ahead)?|promotion\s+go)\b', action, re.I):
            kind = 'approval'
        definition = dict(source_key=source_key, workstream_key=key, owner_lane=owner,
                          source_message_id=source_id, kind=kind, title=action[:500],
                          body=self._request_compat_body(thread, action),
                          resource=resource, completion_rule=rule)
        return definition

    def _request_now_pointer(self, con, thread, now):
        request_id = thread.get('action_request_id')
        if request_id in (None, ''):
            return None
        self._request_uuid(request_id, 'action_request_id')
        key = self._request_key({'workstream_key': thread.get('workstream_key', thread.get('key'))})
        binding = con.execute('SELECT lane_id FROM workstreams WHERE workstream_key=?', (key,)).fetchone()
        if not binding:
            raise self._request_error('workstream has no exact registered owner binding')
        owner = self._request_owner(con, binding['lane_id'])['lane_id']
        if thread.get('owner_lane', owner) != owner:
            raise self._request_error('Board owner_lane does not match the exact workstream binding')
        request = self._request_record(con, request_id)
        if request['workstream_key'] != key or request['owner_lane'] != owner:
            raise self._request_error('action_request_id must identify the exact workstream and owner request')
        if request['compatibility'] and not request.get('owner_managed'):
            con.execute('UPDATE requests SET owner_managed=1,updated_at=? WHERE request_id=?', (now, request_id))
            self._request_audit(con, 'request-linked', dict(request_id=request_id, workstream_key=key, owner_lane=owner), now)
        return request

    def request_reconcile(self, con, data, now):
        if set(data) - {'threads'}:
            raise self._request_error('request-reconcile accepts only trusted threads')
        threads = data.get('threads')
        if threads is not None and (not isinstance(threads, list) or any(not isinstance(item, dict) for item in threads)):
            raise self._request_error('threads must be a complete list of trusted board row objects')
        result = {'created': [], 'preserved': [], 'cancelled': [], 'skipped': [], 'definition_conflicts': []}
        projected = self.conversation_projection(con, now)
        for row in con.execute('SELECT request_id FROM requests').fetchall():
            self._request_reconcile_one(con, row['request_id'], now, projected)
        if threads is not None:
            seen_keys = set()
            for thread in threads:
                key = thread.get('workstream_key', thread.get('key'))
                if isinstance(key, str) and key in seen_keys:
                    raise self._request_error('work board contains duplicate workstream keys')
                if isinstance(key, str):
                    seen_keys.add(key)
            desired = {}
            invalid = set()
            for thread in threads:
                key = thread.get('workstream_key', thread.get('key'))
                try:
                    linked = self._request_now_pointer(con, thread, now)
                    if linked is not None:
                        desired[linked['workstream_key']] = linked['source_key']
                        result['preserved'].append(linked['request_id'])
                        continue
                    definition = self._request_compat_definition(con, thread, now)
                    if definition is None:
                        continue
                    desired[definition['workstream_key']] = definition['source_key']
                    opened = self._request_open(con, definition, now, compatibility=True)
                    result['preserved' if opened['duplicate'] else 'created'].append(opened['request_id'])
                    if opened['definition_conflict']:
                        result['definition_conflicts'].append(opened['request_id'])
                except self.request_error_type as error:
                    if isinstance(key, str):
                        invalid.add(key)
                    result['skipped'].append({'workstream_key': key, 'reason': str(error)})
            for row in con.execute("SELECT * FROM requests WHERE compatibility=1 AND owner_managed=0 AND definition_state='open'").fetchall():
                if (row['workstream_key'] not in invalid and desired.get(row['workstream_key']) != row['source_key']
                        and not self._request_events(con, row['request_id'])):
                    con.execute("UPDATE requests SET definition_state='cancelled',state='cancelled',updated_at=? WHERE request_id=?", (now, row['request_id']))
                    self._request_audit(con, 'request-cancelled', dict(request_id=row['request_id'],
                        source_key=row['source_key'], reason='Explicit board action cleared or replaced'), now)
                    result['cancelled'].append(row['request_id'])
        projected = self.conversation_projection(con, now)
        for row in con.execute('SELECT request_id FROM requests').fetchall():
            self._request_reconcile_one(con, row['request_id'], now, projected)
        snapshot = self.request_list(con, {}, now)
        result.update(requests=snapshot['requests'], counts=snapshot['counts'], truncated=snapshot['truncated'])
        return result

    def request_list(self, con, data, now):
        limit = data.get('request_history_limit', 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 0 <= limit <= 1000:
            raise self._request_error('request_history_limit must be an integer in 0..1000')
        owner = data.get('owner_lane')
        result = dict(requests=[], counts={'requests': 0}, truncated={'requests': False}, request_history_limit=limit)
        if owner is not None:
            if con is None:
                raise self._request_error('lane is not registered: ' + str(owner))
            self.lane(con, owner)
        if con is None or not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'").fetchone():
            return result
        rows = con.execute('SELECT * FROM requests' + (' WHERE owner_lane=?' if owner is not None else ''), (owner,) if owner is not None else ())
        requests = [self._request_projection(con, dict(row), now) for row in rows]
        requests.sort(key=lambda row: (-row['updated_at'], row['request_id']))
        active = [row for row in requests if row['state'] not in TERMINAL]
        active.sort(key=lambda row: (0 if row['state'] in ('open', 'conflict') else 1, -row['updated_at'], row['request_id']))
        terminal = [row for row in requests if row['state'] in TERMINAL]
        result['requests'] = active + terminal[:limit]
        result['counts']['requests'] = len(requests)
        result['truncated']['requests'] = len(terminal) > limit
        return result
