"""Boardagents worker: monitor / orchestrate / resume."""
from core import storage
from core.state_machine import Orchestrator, apply, ACTING_DIRECT, AWAITING_APPROVAL, ESCALATED, CLOSED
from agents import monitor as monitor_agent
from agents import summarizer, decision, action, governance


def _handlers(finding):
    """Handler table for one finding. Board findings deliberate; signals decide."""
    from agents import board
    handlers = {
        'Ingested': summarizer.summarize,
        'Decided': board.route_decided,          # safety net; never left unrouted
        'ActingDirect': action.act,
        'Verifying': _verifying,
        'GovernanceReview': governance.run,
        'Escalated': _escalated_close,
    }
    if (finding or {}).get('mode') == 'board':
        handlers['Summarized'] = board.convene
        handlers['Deliberating'] = board.convene
    else:
        handlers['Summarized'] = decision.decide
    return handlers


def _verifying(finding):
    # In the skeleton the action stage already ran pytest; here we just proceed.
    return governance.run(finding)


def _escalated_close(finding):
    return apply(finding, CLOSED, 'human resolved escalation')


def _sync_meeting(finding):
    """After a board finding settles, close the meeting record and write its minutes."""
    if (finding or {}).get('mode') != 'board':
        return
    mid = finding.get('meeting_id')
    if not mid:
        return
    try:
        from core import meetings
        meeting = meetings.get(mid)
        if not meeting:
            print(f'meeting {mid} not found; skipping minutes')
            return
        board = finding.get('board') or {}
        meetings.update(mid, {
            'finding_id': finding['finding_id'],
            'tally': board.get('tally') or meeting.get('tally') or {},
            'decision': finding.get('decision') or {},
            'participants': board.get('seats') or meeting.get('participants') or [],
            'risk_tag': finding.get('risk_tag'),
        })
        if finding['state'] in (CLOSED, ESCALATED):
            meetings.finalize(mid, status=meetings.ADJOURNED,
                              outcome=f"{finding.get('route')} -> {finding['state']}")
        elif finding['state'] == AWAITING_APPROVAL:
            meetings.finalize(mid, status=meetings.AWAITING_APPROVAL,
                              outcome='draft awaiting the founder')
        meeting = meetings.get(mid)
        key = meetings.write_minutes(meeting, finding)
        print(f'minutes written: {key}')
    except Exception as e:  # minutes must never break the pipeline
        print(f'meeting sync skipped: {type(e).__name__}: {e}')


def orchestrate(finding_id, log=print):
    if storage.is_paused():
        log('paused; skipping orchestration')
        return
    finding = storage.get_finding(finding_id)
    if not finding:
        log(f'finding {finding_id} not found')
        return
    finding = Orchestrator(_handlers(finding), log=log).run(finding)
    _sync_meeting(finding)


def resume(finding_id, approved, note='', log=print):
    finding = storage.get_finding(finding_id)
    if not finding:
        return
    if approved:
        finding['approval'] = {'status': 'approved', 'ts': storage.now_ms(), 'note': note}
        storage.audit(finding_id, 'human', {'action': 'approve', 'note': note})
        _record_meeting_approval(finding, 'approve', note)
        _note_board_override(finding)
        if finding['state'] == ESCALATED:
            apply(finding, CLOSED, 'human resolved the escalation')
        else:
            apply(finding, ACTING_DIRECT, 'human approved; entering action loop')
    else:
        finding['approval'] = {'status': 'rejected', 'ts': storage.now_ms(), 'note': note}
        storage.audit(finding_id, 'human', {'action': 'reject', 'note': note})
        _record_meeting_approval(finding, 'reject', note)
        apply(finding, CLOSED, 'human rejected the draft')


def _record_meeting_approval(finding, action, note):
    if (finding or {}).get('mode') != 'board' or not finding.get('meeting_id'):
        return
    try:
        from core import meetings
        meetings.record_approval(finding['meeting_id'], action, note)
    except Exception as e:
        print(f'meeting approval not recorded: {type(e).__name__}: {e}')


def _note_board_override(finding):
    """If the founder approves something their own board voted against, that is minuted.

    A founder may overrule their board - it is their company - but the record must show
    that the decision came from the human against the board's recommendation.
    """
    if (finding or {}).get('mode') != 'board':
        return
    tally = (finding.get('board') or {}).get('tally') or {}
    aye = int(tally.get('aye', 0))
    nay = int(tally.get('nay', 0))
    support = aye + int(tally.get('conditional', 0))
    if nay <= support:
        return
    storage.audit(finding['finding_id'], 'human',
                  {'action': 'approve', 'overrode_board': True, 'tally': tally,
                   'note': 'the founder approved against the board recommendation'})


def lambda_handler(event, context):
    kind = (event or {}).get('kind')
    log = lambda *a: print(*a)
    if kind == 'monitor':
        made = monitor_agent.run_monitor()
        for fid in made:
            orchestrate(fid, log=log)
        return {'made': made}
    if kind == 'orchestrate':
        fid = event['finding_id']
        storage.audit(fid, 'event', {'orchestrator': 'started'})
        orchestrate(fid, log=log)
        return {'finding_id': fid}
    if kind == 'resume':
        resume(event['finding_id'], event.get('approved', False),
               note=event.get('note', ''), log=log)
        orchestrate(event['finding_id'], log=log) if event.get('approved') else _sync_meeting(
            storage.get_finding(event['finding_id']))
        return {'finding_id': event['finding_id']}
    return {'error': f'unknown kind {kind}'}
