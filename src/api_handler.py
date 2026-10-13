"""Boardagents API: dashboard + JSON endpoints on a public Function URL."""
import json
import os
import time
import urllib

from core import storage
from core.config import cfg

_here = os.path.dirname(__file__)
DASHBOARD = open(os.path.join(_here, 'dashboard.html')).read() if os.path.exists(os.path.join(_here, 'dashboard.html')) else '<h1>boardagents</h1>'


def _resp(code, body, content_type='application/json'):
    return {'statusCode': code, 'headers': {'Content-Type': content_type, 'Access-Control-Allow-Origin': '*',
                                            'Cache-Control': 'no-store'}, 'body': body}


def _ok(obj):
    return _resp(200, json.dumps(obj, default=str))


def _err(code, msg):
    return _resp(code, json.dumps({'error': msg}))


def _async_worker(finding_id):
    import boto3
    name = cfg()['worker_name']
    boto3.client('lambda').invoke(FunctionName=name, InvocationType='Event',
                                  Payload=json.dumps({'kind': 'orchestrate', 'finding_id': finding_id}))


def _check_token(headers, body):
    token = (headers or {}).get('x-boardagents-token') or (body or {}).get('token', '')
    expected = cfg()['admin_token']
    if expected and token != expected:
        return False
    return True


def _inject(scenario):
    # Public but capped: 30 injections/day, 10s cooldown, one active finding per scenario.
    count, day = storage.incr_daily_counter('inject', 30)
    if count > 30:
        return _err(429, 'daily injection cap reached')
    feed = storage._ddb()['feed']
    items = feed.scan().get('Items', [])
    active = [i for i in items if i.get('scenario') == scenario and not i.get('processed')]
    if active:
        return _err(409, 'scenario already active')
    recent = [i for i in items if i.get('scenario') == scenario and (storage.now_ms() - int(i.get('created_at', 0))) < 10000]
    if recent:
        return _err(429, 'cooldown: retry in a few seconds')
    from agents.monitor import classify_event_type
    from agents import monitor
    event = SCENARIOS[scenario]()
    storage.put_feed_event(event)
    # kick the monitor worker directly; the EventBridge schedule is a backup path
    import boto3
    boto3.client('lambda').invoke(FunctionName=cfg()['worker_name'], InvocationType='Event',
                                  Payload=b'{"kind":"monitor"}')
    return _ok({'queued': event['event_id']})


def _scenario_event(scenario):
    import uuid
    from agents.monitor import classify_event_type
    now = storage.now_ms()
    if scenario == 'A':
        return {'event_id': 'e-' + uuid.uuid4().hex[:10], 'scenario': 'A', 'source': 'dependabot', 'created_at': now,
                'event_type': 'dependency_advisory',
                'payload': {'title': 'mailkit 1.x EOL + CVE-2026-1337', 'package': 'vendor/mailkit',
                            'severity': 'high', 'fix': 'upgrade to mailkit 2.x', 'cve': 'CVE-2026-1337'}}
    if scenario == 'B':
        return {'event_id': 'e-' + uuid.uuid4().hex[:10], 'scenario': 'B', 'source': 'billing-webhooks', 'created_at': now,
                'event_type': 'billing_anomaly',
                'payload': {'title': 'Duplicate charges spike', 'customers_affected': 3,
                            'pattern': 'same customer+amount within 90s', 'suspected_cause': 'webhook retries without idempotency'}}
    if scenario == 'C':
        return {'event_id': 'e-' + uuid.uuid4().hex[:10], 'scenario': 'C', 'source': 'analytics', 'created_at': now,
                'event_type': 'usage_anomaly',
                'payload': {'title': 'Signups -12% on one day', 'delta_pct': -12, 'context': 'national holiday', 'data_points': 1}}
    return None


SCENARIOS = {'A': lambda: _scenario_event('A'), 'B': lambda: _scenario_event('B'), 'C': lambda: _scenario_event('C')}


def _prior_rounds(thread_id, limit=4):
    """Earlier sessions in this thread - the board's memory of what it already said."""
    from core import meetings
    out = []
    for m in meetings.thread_meetings(thread_id):
        if m.get('status') == meetings.CONVENED:
            continue
        out.append({
            'round': m.get('round'),
            'topic': (m.get('topic') or '')[:400],
            'status': m.get('status'),
            'outcome': m.get('outcome'),
            'motion': (m.get('resolution') or {}).get('motion'),
            'tally': m.get('tally'),
        })
    return out[-limit:]


def _convene_founder(body):
    """A founder topic. Any text at all - a question, a worry, a call transcript."""
    import uuid
    from core import meetings, context
    from core import policy as policy_mod

    message = (body.get('message') or '').strip()
    if not message:
        return _err(400, 'message is required')
    if len(message) > 4000:
        return _err(413, 'message too long: 4000 characters maximum')

    count, _day = storage.incr_daily_counter('board', 30)
    if count > 30:
        return _err(429, 'daily board-session cap reached')

    thread_id = (body.get('thread_id') or '').strip()
    if thread_id:
        rounds = meetings.thread_round(thread_id)
        if rounds > meetings.ROUND_CAP:
            return _err(429, f'this thread has reached its {meetings.ROUND_CAP}-round cap')

    profile = context.operator_profile()
    category, basis = policy_mod.topic_basis(message)
    meeting = meetings.create(
        message,
        thread_id=thread_id or None,
        risk_tag=policy_mod.risk_for_topic(message),
        category=category,
        operator={'company': profile.get('company'), 'team_size': profile.get('team_size'),
                  'stage': profile.get('stage')},
        seats=[{'role': seat['role'], 'seat': seat['stage']} for seat in _board_seats()],
    )
    event = {
        'event_id': 'e-' + uuid.uuid4().hex[:10],
        'source': 'founder',
        'mode': 'board',
        'meeting_id': meeting['meeting_id'],
        'thread_id': meeting['thread_id'],
        'created_at': storage.now_ms(),
        'payload': {
            'message': message,
            'round': meeting['round'],
            'risk_basis': basis,
            'prior_rounds': _prior_rounds(meeting['thread_id']),
        },
    }
    storage.put_feed_event(event)
    import boto3
    boto3.client('lambda').invoke(FunctionName=cfg()['worker_name'], InvocationType='Event',
                                  Payload=b'{"kind":"monitor"}')
    return _ok({'meeting_id': meeting['meeting_id'], 'thread_id': meeting['thread_id'],
                'round': meeting['round'], 'risk_tag': meeting['risk_tag'],
                'topic_category': category, 'risk_basis': basis})


def _board_seats():
    from agents.board import BOARD
    return BOARD


def _meeting_summary(m):
    return {k: m.get(k) for k in ('meeting_id', 'thread_id', 'round', 'topic', 'status', 'outcome',
                                  'risk_tag', 'topic_category', 'convened_at', 'adjourned_at',
                                  'finding_id', 'minutes_s3_key', 'cost', 'tally', 'participants')}


def lambda_handler(event, context):
    method = (event.get('requestContext', {}).get('http') or {}).get('method', 'GET')
    path = (event.get('requestContext', {}).get('http') or {}).get('path', '/')
    qs = event.get('queryStringParameters') or {}
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    try:
        body = json.loads(event.get('body') or '{}') if method == 'POST' else {}
    except ValueError:
        return _err(400, 'bad json body')

    if method == 'GET' and path in ('/', '/index.html'):
        return _resp(200, DASHBOARD, 'text/html; charset=utf-8')
    if method == 'GET' and path == '/api/findings':
        return _ok({'findings': storage.list_findings(), 'paused': storage.is_paused()})
    if method == 'GET' and path.startswith('/api/findings/'):
        fid = path.split('/')[-1]
        finding = storage.get_finding(fid)
        if not finding:
            return _err(404, 'not found')
        finding['audit'] = storage.audit_for(fid)
        return _ok(finding)
    if method == 'POST' and path == '/api/inject':
        s = body.get('scenario', '').upper()
        if s not in SCENARIOS:
            return _err(400, 'scenario must be A, B or C')
        return _inject(s)
    if method == 'POST' and path == '/api/board':
        return _convene_founder(body)
    if method == 'GET' and path == '/api/meetings':
        from core import meetings
        return _ok({'meetings': [_meeting_summary(m) for m in meetings.all_meetings()]})
    if method == 'GET' and path.startswith('/api/threads/'):
        from core import meetings
        tid = path.split('/')[-1]
        return _ok({'thread_id': tid,
                    'meetings': [_meeting_summary(m) for m in meetings.thread_meetings(tid)]})
    if method == 'GET' and path.startswith('/api/meetings/'):
        from core import meetings
        parts = [p for p in path.split('/') if p]
        mid = parts[2] if len(parts) > 2 else ''
        if not mid:
            return _err(400, 'meeting id required')
        meeting = meetings.get(mid)
        if not meeting:
            return _err(404, 'meeting not found')
        fid = meeting.get('finding_id') or ''
        finding = storage.get_finding(fid) if fid else None
        if len(parts) >= 4 and parts[3] == 'minutes.md':
            return _resp(200, meetings.minutes_markdown(meeting, finding or {}),
                         'text/markdown; charset=utf-8')
        out = dict(meeting)
        out['finding'] = finding
        out['audit'] = storage.audit_for(fid) if fid else []
        return _ok(out)
    if method == 'POST' and path.endswith('/approve'):
        fid = path.split('/')[-2]
        _token_ok = _check_token(headers, body)
        import boto3
        boto3.client('lambda').invoke(FunctionName=cfg()['worker_name'], InvocationType='Event',
                                      Payload=json.dumps({'kind': 'resume', 'finding_id': fid, 'approved': True, 'note': body.get('note', '')}))
        return _ok({'resuming': fid})
    if method == 'POST' and path.endswith('/reject'):
        fid = path.split('/')[-2]
        import boto3
        boto3.client('lambda').invoke(FunctionName=cfg()['worker_name'], InvocationType='Event',
                                      Payload=json.dumps({'kind': 'resume', 'finding_id': fid, 'approved': False, 'note': body.get('note', '')}))
        return _ok({'resuming': fid})
    if method == 'POST' and path in ('/api/pause', '/api/resume', '/api/reset'):
        if not _check_token(headers, body):
            return _err(401, 'admin token required')
        if path == '/api/pause':
            storage.set_paused(True)
            return _ok({'paused': True})
        if path == '/api/resume':
            storage.set_paused(False)
            return _ok({'paused': False})
        storage.clear_tables()
        return _ok({'reset': True})
    return _err(404, 'not found')
