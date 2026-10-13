"""Meeting records: the permanent, exportable track of every board session.

The audit table stays the source of truth (append-only). A meeting is a queryable
projection so the founder can list sessions, re-read minutes, and see who dissented,
what was decided, who approved it and what it cost.

Sizing note: this table is deliberately tiny (one row per board session), so listing
scans it rather than paying for a GSI - the same call already made for the feed table.
"""
import json
import uuid

from core import storage

CONVENED = 'convened'
IN_SESSION = 'in_session'
AWAITING_APPROVAL = 'awaiting_approval'
ADJOURNED = 'adjourned'

ROUND_CAP = 12  # a thread may not be re-opened endlessly; keeps the demo inside caps


def new_meeting_id():
    return 'm-' + uuid.uuid4().hex[:10]


def _table():
    return storage._ddb()['meetings']


def _to_ddb(obj):
    return storage._to_ddb(obj)


def _from_ddb(obj):
    return storage._from_ddb(obj)


def all_meetings(limit=50):
    items = _from_ddb(_table().scan().get('Items', []))
    items.sort(key=lambda m: m.get('convened_at') or 0, reverse=True)
    return items[:limit]


def thread_meetings(thread_id):
    items = [m for m in all_meetings(limit=500) if m.get('thread_id') == thread_id]
    items.sort(key=lambda m: m.get('convened_at') or 0)
    return items


def thread_round(thread_id):
    return len(thread_meetings(thread_id)) + 1


def get(meeting_id):
    resp = _table().get_item(Key={'meeting_id': meeting_id})
    return _from_ddb(resp['Item']) if resp.get('Item') else None


def create(topic, finding_id='', thread_id=None, risk_tag='sensitive', category='unknown',
           operator=None, seats=None):
    """Opens a session. The agenda is the founder's own words, quoted."""
    mid = new_meeting_id()
    thread = thread_id or mid
    ts = storage.now_ms()
    item = {
        'meeting_id': mid,
        'thread_id': thread,
        'round': thread_round(thread),
        'topic': topic,
        'finding_id': finding_id,
        'status': CONVENED,
        'outcome': '',
        'risk_tag': risk_tag,
        'topic_category': category,
        'convened_at': ts,
        'updated_at': ts,
        'adjourned_at': None,
        'operator': operator or {},
        'agenda': [{'item': topic, 'raised_by': 'founder', 'at': ts}],
        'participants': seats or [],
        'turns': [],
        'tally': {},
        'resolution': {},
        'decision': {},
        'action_items': [],
        'approvals': [],
        'cost': {'tokens_in': 0, 'tokens_out': 0, 'latency_ms': 0},
    }
    _table().put_item(Item=_to_ddb(item))
    return item


def update(meeting_id, updates):
    """Shallow-merge updates onto the meeting row."""
    expr = 'SET ' + ', '.join(f'#{i} = :v{i}' for i in range(len(updates)))
    names = {f'#{i}': k for i, k in enumerate(updates)}
    values = {f':v{i}': _to_ddb(v) for i, v in enumerate(updates.values())}
    _table().update_item(Key={'meeting_id': meeting_id},
                         UpdateExpression=expr,
                         ExpressionAttributeNames=names,
                         ExpressionAttributeValues=values)


def record_turn(finding, turn):
    """Appends one speaker's turn to the meeting, with its cost."""
    mid = (finding or {}).get('meeting_id')
    if not mid:
        return None
    _table().update_item(
        Key={'meeting_id': mid},
        UpdateExpression=('SET #t = list_append(if_not_exists(#t, :empty), :vals), '
                          '#u = :ts, #st = :st'),
        ExpressionAttributeNames={'#t': 'turns', '#u': 'updated_at', '#st': 'status'},
        ExpressionAttributeValues={
            ':vals': _to_ddb([turn]), ':empty': [],
            ':ts': storage.now_ms(), ':st': IN_SESSION,
        },
    )
    _add_cost(mid, turn)
    return mid


def record_resolution(finding, resolution):
    mid = (finding or {}).get('meeting_id')
    if not mid:
        return None
    update(mid, {'resolution': resolution})
    _add_cost(mid, resolution)
    return mid


def _add_cost(meeting_id, turn):
    """Rolls one model call's tokens and latency into the meeting total, atomically."""
    deltas = {'tokens_in': int(turn.get('tokens_in', 0) or 0),
              'tokens_out': int(turn.get('tokens_out', 0) or 0),
              'latency_ms': int(turn.get('latency_ms', 0) or 0)}
    sets = []
    names, values = {'#c': 'cost'}, {}
    for i, (key, delta) in enumerate(deltas.items()):
        names[f'#k{i}'] = key
        values[f':z{i}'] = 0
        values[f':d{i}'] = delta
        sets.append(f'#c.#k{i} = if_not_exists(#c.#k{i}, :z{i}) + :d{i}')
    _table().update_item(
        Key={'meeting_id': meeting_id},
        UpdateExpression='SET ' + ', '.join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def finalize(meeting_id, status=ADJOURNED, outcome=''):
    update(meeting_id, {'status': status, 'outcome': outcome,
                        'adjourned_at': storage.now_ms(), 'updated_at': storage.now_ms()})


def _ts(value):
    if not value:
        return '-'
    import time
    try:
        return time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(int(value) / 1000))
    except (TypeError, ValueError):
        return str(value)


def minutes_markdown(meeting, detail=None):
    """The permanent record, rendered from the meeting row and its finding."""
    m = meeting or {}
    finding = detail or {}
    board = finding.get('board') or {}
    decision = finding.get('decision') or {}
    lines = []
    lines.append(f"# Board meeting minutes — {(m.get('operator') or {}).get('company', 'the company')}")
    lines.append('')
    lines.append(f"- **Meeting id:** `{m.get('meeting_id')}`")
    lines.append(f"- **Thread:** `{m.get('thread_id')}` · round {m.get('round', 1)}")
    lines.append(f"- **Convened:** {_ts(m.get('convened_at'))}")
    lines.append(f"- **Adjourned:** {_ts(m.get('adjourned_at'))}")
    lines.append(f"- **Status:** {m.get('status')} · **Outcome:** {m.get('outcome') or 'recorded'}")
    lines.append('')

    lines.append('## Present')
    lines.append('')
    lines.append('| Seat | Model | Vote | Confidence |')
    lines.append('|---|---|---|---|')
    for turn in m.get('turns') or []:
        conf = turn.get('confidence')
        lines.append(f"| {turn.get('role')} | `{turn.get('model_id')}` | {turn.get('vote')} | "
                     f"{conf if conf is None else round(float(conf), 2)} |")
    lines.append('')

    lines.append('## Agenda')
    lines.append('')
    for item in m.get('agenda') or []:
        lines.append(f"1. {item.get('item')}")
    lines.append('')

    lines.append('## Deliberation')
    lines.append('')
    for turn in m.get('turns') or []:
        lines.append(f"### {turn.get('role')} — `{turn.get('model_id')}`")
        lines.append('')
        lines.append(f"**Vote:** {turn.get('vote')} · **Confidence:** {turn.get('confidence')}")
        lines.append('')
        lines.append(f"**Position.** {turn.get('position')}")
        lines.append('')
        if turn.get('reasoning'):
            lines.append(f"{turn.get('reasoning')}")
            lines.append('')
        for ev in turn.get('evidence') or []:
            lines.append(f"- evidence `{ev.get('ref')}`: \"{ev.get('quote')}\"")
        for concern in turn.get('concerns') or []:
            lines.append(f"- concern: {concern}")
        if turn.get('ungrounded_refs'):
            lines.append(f"- **ungrounded citations:** {turn['ungrounded_refs']}")
        lines.append('')

    lines.append('## Vote as cast')
    lines.append('')
    tally = m.get('tally') or board.get('tally') or {}
    if tally:
        lines.append(' · '.join(f"**{k}** {tally.get(k, 0)}"
                                for k in ('aye', 'nay', 'conditional', 'abstain')))
        lines.append('')

    resolution = m.get('resolution') or {}
    lines.append('## Resolution')
    lines.append('')
    lines.append(f"**Motion:** {resolution.get('motion') or board.get('motion')}")
    lines.append('')
    lines.append(f"{resolution.get('recommendation') or decision.get('rationale') or ''}")
    lines.append('')
    for item in resolution.get('consensus') or board.get('consensus') or []:
        lines.append(f"- agreed: {item}")
    for item in resolution.get('dissent') or board.get('dissent') or []:
        lines.append(f"- **dissent:** {item}")
    lines.append('')

    lines.append('## Decision and policy')
    lines.append('')
    lines.append(f"- **Risk tag:** {m.get('risk_tag')} (from topic category `{m.get('topic_category')}`)")
    lines.append(f"- **Policy route:** {finding.get('route')}")
    lines.append(f"- **Chair confidence (model):** {decision.get('model_confidence')}")
    lines.append(f"- **Chair confidence (after deterministic ceilings):** {decision.get('confidence')}")
    lines.append(f"- **Policy overruled the board:** "
                 f"{'yes' if board.get('ungrounded_total') is None else 'see audit'}")
    for item in (finding.get('attempts') or []):
        lines.append(f"- attempt {item.get('attempt')}: exit={item.get('exit_code')} "
                     f"passed={item.get('passed')} files={item.get('applied_files')}")
    gov = finding.get('governance') or {}
    if gov:
        lines.append(f"- governance verdict: **{gov.get('verdict')}** "
                     f"({sum(1 for c in gov.get('checks', []) if c.get('pass'))}/"
                     f"{len(gov.get('checks', []))} checks passed)")
    lines.append('')

    approvals = m.get('approvals') or []
    lines.append('## Approvals')
    lines.append('')
    if approvals:
        for a in approvals:
            lines.append(f"- {a.get('action')} by {a.get('by')} at {_ts(a.get('ts'))}"
                         f"{(' — ' + a['note']) if a.get('note') else ''}")
    else:
        lines.append('- none required')
    lines.append('')

    cost = m.get('cost') or {}
    lines.append('## Cost')
    lines.append('')
    lines.append(f"- tokens in/out: {cost.get('tokens_in', 0)} / {cost.get('tokens_out', 0)}")
    lines.append(f"- model latency total: {cost.get('latency_ms', 0)} ms")
    lines.append('')
    lines.append('---')
    lines.append('_Generated by Boardagents from the append-only audit log. '
                 'Every model call above is recorded with its model id, token count and latency._')
    return '\n'.join(lines)


def record_approval(meeting_id, action, note='', by='human'):
    """Appends a human decision to the meeting record and returns the entry."""
    entry = {'by': by, 'action': action, 'note': note, 'ts': storage.now_ms()}
    _table().update_item(
        Key={'meeting_id': meeting_id},
        UpdateExpression='SET #a = list_append(if_not_exists(#a, :empty), :vals), #u = :ts',
        ExpressionAttributeNames={'#a': 'approvals', '#u': 'updated_at'},
        ExpressionAttributeValues={':vals': _to_ddb([entry]), ':empty': [], ':ts': storage.now_ms()},
    )
    return entry


def write_minutes(meeting, finding=None):
    """Stores the minutes in S3 and returns the key."""
    mid = meeting.get('meeting_id')
    key = f'meetings/{mid}/minutes.md'
    storage.put_artifact(key, minutes_markdown(meeting, finding))
    update(mid, {'minutes_s3_key': key})
    return key
