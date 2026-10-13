"""Board room: the founder puts something to the board and the board deliberates.

Real, not staged. Every seat speaks in turn and hears the seats before it, and every
seat runs on its own model (see core/config.MODEL_CONFIG) so the audit trail shows
genuine multi-model routing. The vote tally is counted by this code, never by a model.
Evidence must resolve into the deterministic BoardContext (core/context.py) or it is
recorded as a hallucination and counted against that speaker.
"""
import json
import os

from core import context, policy, router, storage
from core.state_machine import (apply, ACTING_DIRECT, AWAITING_APPROVAL, DECIDED,
                               DELIBERATING, ESCALATED, SUMMARIZED)

_here = os.path.dirname(os.path.abspath(__file__))

# Four functional seats plus a chair. Each has one lens and no other.
BOARD = [
    {'key': 'cto', 'stage': 'board_cto', 'role': 'CTO',
     'lens': ('Engineering reality: what a change actually touches, what breaks, what it costs to '
              'build, what is hard to reverse. You are the one who has to maintain it afterwards.')},
    {'key': 'cfo', 'stage': 'board_cfo', 'role': 'CFO',
     'lens': ('Money: revenue, cost, burn, runway, unit economics. You quantify in the currency of '
              'the company and you are sceptical of any spend without a return.')},
    {'key': 'risk', 'stage': 'board_risk', 'role': 'Risk & Compliance',
     'lens': ('Downside: what could go wrong, who could be harmed, what is irreversible, legal and '
              'contractual exposure, customer data and credentials.')},
    {'key': 'growth', 'stage': 'board_growth', 'role': 'Customer & Growth',
     'lens': ('The customer: what they would actually experience, whether they would even notice, '
              'churn and retention, and how it would be communicated.')},
]
CHAIR = {'key': 'chair', 'stage': 'board_chair', 'role': 'Chair'}

VOTES = ('aye', 'conditional', 'nay', 'abstain')
SUGGESTIONS = ('act_direct', 'draft_for_approval', 'escalate')
MEMBER_PROMPT = 'board_member.md'
CHAIR_PROMPT = 'board_chair.md'


# ---------------------------------------------------------------- prompts

def load_prompt(name):
    """Prompts are versioned in prompts/ and bundled next to the code at deploy time."""
    for candidate in (os.path.join(_here, '..', 'prompts', name),
                      os.path.join(_here, '..', '..', 'prompts', name)):
        if os.path.exists(candidate):
            with open(candidate) as fh:
                return fh.read()
    raise FileNotFoundError(f'prompt not found: {name}')


def _fill(template, mapping):
    out = template
    for key, value in mapping.items():
        out = out.replace('{{' + key + '}}', str(value))
    return out


# ---------------------------------------------------------------- validation

def validate_member(obj):
    assert isinstance(obj.get('position'), str) and obj['position'].strip(), 'position required'
    assert isinstance(obj.get('reasoning'), str) and obj['reasoning'].strip(), 'reasoning required'
    assert isinstance(obj.get('confidence'), (int, float)) and 0 <= float(obj['confidence']) <= 1, \
        'confidence must be 0..1'
    assert obj.get('vote') in VOTES, f"vote must be one of {VOTES}"
    assert isinstance(obj.get('evidence', []), list), 'evidence must be a list'
    assert isinstance(obj.get('concerns', []), list), 'concerns must be a list'
    for e in obj.get('evidence', []):
        assert isinstance((e or {}).get('ref'), str), 'evidence.ref must be a string'


def validate_chair(obj):
    assert isinstance(obj.get('motion'), str) and obj['motion'].strip(), 'motion required'
    assert isinstance(obj.get('recommendation'), str) and obj['recommendation'].strip(), \
        'recommendation required'
    assert isinstance(obj.get('confidence'), (int, float)) and 0 <= float(obj['confidence']) <= 1
    assert obj.get('suggested_action') in SUGGESTIONS, f'suggested_action must be one of {SUGGESTIONS}'
    brief = obj.get('action_brief') or {}
    assert isinstance(brief.get('target_paths', []), list), 'action_brief.target_paths must be a list'
    assert isinstance(obj.get('dissent', []), list), 'dissent must be a list'
    assert isinstance(obj.get('consensus', []), list), 'consensus must be a list'


def tally_votes(turns):
    """Counted here, deterministically. A model never counts its own vote."""
    out = {v: 0 for v in VOTES}
    for turn in turns or []:
        vote = (turn or {}).get('vote')
        if vote in out:
            out[vote] += 1
    out['cast'] = len(turns or [])
    return out


def transcript(turns, universe):
    """What a speaker has heard so far, with each claim reduced to a citable ref."""
    lines = []
    for turn in turns or []:
        lines.append({
            'role': turn.get('role'),
            'model_id': turn.get('model_id'),
            'position': turn.get('position'),
            'vote': turn.get('vote'),
            'confidence': turn.get('confidence'),
            'concerns': turn.get('concerns', []),
            'evidence_refs': [e.get('ref') for e in (turn.get('evidence') or [])],
        })
    return json.dumps(lines, indent=1, sort_keys=True, default=str)


# ---------------------------------------------------------------- idempotency

def existing_turns(finding_id):
    """Turns already recorded. Makes a re-run of the same stage a no-op."""
    seen = {}
    for row in storage.audit_for(finding_id):
        kind = row.get('kind')
        payload = row.get('payload') or {}
        if kind == 'board_turn' and payload.get('key'):
            seen[payload['key']] = payload
        elif kind == 'board_resolution':
            seen['chair'] = payload
    return seen


# ---------------------------------------------------------------- speaking

def _speak(finding, seat, universe, turns, tally, log):
    """One seat, one model, one audited turn. Returns (turn, error)."""
    company = context.operator_profile().get('company') or 'the company'
    system = (f"You are the {seat['role']} on the board of {company}. "
              'You stay strictly inside your lens. Reply with JSON only.')
    user = _fill(load_prompt(MEMBER_PROMPT), {
        'ROLE': seat['role'],
        'COMPANY': company,
        'LENS': seat['lens'],
        'AGENDA': (finding.get('payload') or {}).get('message', ''),
        'CONTEXT': context.render(universe),
        'TRANSCRIPT': transcript(turns, universe),
        'TALLY': json.dumps(tally, sort_keys=True),
    })
    resp = router.call_llm(seat['stage'], router.LLMRequest(system, user),
                           finding_id=finding['finding_id'],
                           ctx={'universe_keys': sorted(universe),
                                'payload': finding.get('payload') or {}},
                           log=log)
    try:
        obj = router.extract_json(resp.text, [validate_member])
    except ValueError as e:
        return None, str(e)

    model_confidence = float(obj['confidence'])
    ungrounded = context.ungrounded_refs(obj.get('evidence'), universe)
    confidence = model_confidence
    if ungrounded:
        confidence = min(confidence, 0.5)
        storage.audit(finding['finding_id'], 'constraint',
                      {'stage': seat['stage'], 'guard': 'grounded_evidence',
                       'speaker': seat['role'], 'ungrounded_refs': ungrounded,
                       'model_confidence': model_confidence, 'applied_confidence': confidence})

    usage = resp.usage or {}
    turn = {
        'key': seat['key'], 'role': seat['role'], 'seat': seat['stage'],
        'model_id': resp.model_id,
        'position': obj['position'], 'reasoning': obj['reasoning'],
        'vote': obj['vote'], 'confidence': confidence, 'model_confidence': model_confidence,
        'evidence': obj.get('evidence', []), 'concerns': obj.get('concerns', []),
        'recommends_action': bool(obj.get('recommends_action')),
        'ungrounded_refs': ungrounded,
        'latency_ms': resp.latency_ms,
        'tokens_in': usage.get('prompt_tokens', 0),
        'tokens_out': usage.get('completion_tokens', 0),
    }
    log(f"{seat['role']} voted {turn['vote']} at {confidence:.2f} ({resp.model_id})")
    storage.audit(finding['finding_id'], 'board_turn', turn)
    return turn, None


# ---------------------------------------------------------------- the chair

def _convene_chair(finding, universe, turns, tally, log):
    company = context.operator_profile().get('company') or 'the company'
    system = (f'You are the Chair of the board of {company}. You have no lens of your own: '
              'weigh the members, name the disagreement honestly, and put a proposal. '
              'Reply with JSON only.')
    user = _fill(load_prompt(CHAIR_PROMPT), {
        'COMPANY': company,
        'AGENDA': (finding.get('payload') or {}).get('message', ''),
        'CONTEXT': context.render(universe),
        'TRANSCRIPT': transcript(turns, universe),
        'TALLY': json.dumps(tally, sort_keys=True),
    })
    resp = router.call_llm(CHAIR['stage'], router.LLMRequest(system, user),
                           finding_id=finding['finding_id'],
                           ctx={'universe_keys': sorted(universe),
                                'payload': finding.get('payload') or {}},
                           log=log)
    try:
        obj = router.extract_json(resp.text, [validate_chair])
    except ValueError as e:
        return None, str(e)
    usage = resp.usage or {}
    resolution = {**obj, 'key': 'chair', 'role': CHAIR['role'], 'seat': CHAIR['stage'],
                  'model_id': resp.model_id, 'model_confidence': float(obj['confidence']),
                  'latency_ms': resp.latency_ms,
                  'tokens_in': usage.get('prompt_tokens', 0),
                  'tokens_out': usage.get('completion_tokens', 0)}
    log(f"chair: {resolution.get('suggested_action')} at {resolution['confidence']} ({resp.model_id})")
    storage.audit(finding['finding_id'], 'board_resolution', resolution)
    return resolution, None


def _record_turn(finding, turn):
    """Projection into the meetings table; the audit log remains the source of truth."""
    try:
        from core import meetings
        meetings.record_turn(finding, turn)
    except Exception as e:  # never let the projection break the deliberation
        print(f'meeting record update skipped: {type(e).__name__}: {e}')


def _record_resolution(finding, resolution):
    try:
        from core import meetings
        meetings.record_resolution(finding, resolution)
    except Exception as e:
        print(f'meeting resolution not recorded: {type(e).__name__}: {e}')


def _brief_for_finding(finding, resolution):
    """Whose runbook owns the blast radius: a known code playbook, else the scope guard."""
    try:
        import scenarios as scen
        playbook = scen.brief_for(finding.get('scenario'))
    except Exception:
        playbook = {'goal': '', 'target_paths': []}
    if playbook.get('target_paths'):
        proposed = list((resolution.get('action_brief') or {}).get('target_paths') or [])
        if proposed != playbook['target_paths']:
            storage.audit(finding['finding_id'], 'constraint',
                          {'stage': CHAIR['stage'], 'guard': 'playbook_scope',
                           'model_proposed_paths': proposed,
                           'applied_paths': playbook['target_paths'],
                           'reason': 'the operator playbook for this topic owns target_paths'})
        goal = playbook.get('goal') or (resolution.get('action_brief') or {}).get('goal', '')
        return {'goal': goal, 'target_paths': playbook['target_paths']}
    return _scope_guard(finding, resolution)


def _scope_guard(finding, resolution):
    """A board may only propose changes to paths that really exist in the project."""
    facts = context.repo_facts()
    allowed = set(facts.get('tree') or []) | set(facts.get('tests') or [])
    allowed |= set((facts.get('key_files') or {}).keys())
    brief = dict(resolution.get('action_brief') or {})
    proposed = list(brief.get('target_paths') or [])
    kept = [p for p in proposed if p in allowed]
    if kept != proposed:
        storage.audit(finding['finding_id'], 'constraint',
                      {'stage': CHAIR['stage'], 'guard': 'existing_paths_only',
                       'proposed': proposed, 'kept': kept,
                       'dropped': [p for p in proposed if p not in allowed],
                       'reason': 'target_paths may only name files that exist in the real project'})
    return {'goal': brief.get('goal', ''), 'target_paths': kept}


# ---------------------------------------------------------------- the session

def convene(finding, log=print):
    """Orchestrator handler for Summarized (board findings). Idempotent per speaker."""
    fid = finding['finding_id']
    if finding['state'] not in (SUMMARIZED, DELIBERATING):
        # The session has already moved on; re-entering must not re-apply transitions.
        log(f'{fid}: nothing to deliberate, finding is {finding["state"]}')
        return finding
    if finding['state'] == SUMMARIZED:
        finding = apply(finding, DELIBERATING, 'board convened; the founder has the floor')

    universe = context.evidence_universe(finding)
    recorded = existing_turns(fid)
    turns = [recorded[seat['key']] for seat in BOARD if seat['key'] in recorded]

    for seat in BOARD:
        if seat['key'] in recorded:
            log(f"{seat['role']} already on the record; not reconvening")
            continue
        turn, err = _speak(finding, seat, universe, turns, tally_votes(turns), log)
        if err:
            storage.audit(fid, 'error', {'stage': seat['stage'], 'role': seat['role'], 'error': err})
            return apply(finding, ESCALATED, f"board member {seat['role']} could not be heard: {err}")
        turns.append(turn)
        _record_turn(finding, turn)

    resolution = recorded.get('chair')
    if not resolution:
        resolution, err = _convene_chair(finding, universe, turns, tally_votes(turns), log)
        if err:
            storage.audit(fid, 'error', {'stage': CHAIR['stage'], 'error': err})
            return apply(finding, ESCALATED, f'chair failed to put a motion: {err}')
        _record_resolution(finding, resolution)

    return _resolve(finding, resolution, turns, log)


def _resolve(finding, resolution, turns, log):
    """Deterministic ceilings, then the ordinary policy engine. The board only suggests."""
    fid = finding['finding_id']
    tally = tally_votes(turns)
    ungrounded_total = sum(len(t.get('ungrounded_refs') or []) for t in turns)

    confidence = float(resolution['confidence'])
    ceiling, reason = policy.board_confidence_ceiling(tally, ungrounded_total)
    if confidence > ceiling:
        storage.audit(fid, 'constraint',
                      {'stage': CHAIR['stage'], 'guard': 'board_ceiling',
                       'model_confidence': confidence, 'applied_confidence': ceiling,
                       'reason': reason, 'tally': tally, 'ungrounded_total': ungrounded_total})
        confidence = ceiling

    brief = _brief_for_finding(finding, resolution)
    route = policy.route(confidence, finding['risk_tag'])
    if policy.check_override(resolution.get('suggested_action'), route):
        storage.audit(fid, 'policy_override',
                      {'model_suggested': resolution.get('suggested_action'), 'policy_route': route,
                       'risk_tag': finding['risk_tag'], 'confidence': confidence, 'source': 'board'})

    finding['decision'] = {
        'suggested_action': resolution.get('suggested_action'),
        'rationale': resolution.get('recommendation', ''),
        'confidence': confidence,
        'model_confidence': float(resolution['confidence']),
        'action_brief': brief,
        'source': 'board',
    }
    finding['board'] = {
        'motion': resolution.get('motion'),
        'tally': tally,
        'consensus': resolution.get('consensus', []),
        'dissent': resolution.get('dissent', []),
        'seats': [{'role': t.get('role'), 'model_id': t.get('model_id'), 'vote': t.get('vote'),
                   'confidence': t.get('confidence')} for t in turns],
        'ungrounded_total': ungrounded_total,
        'minutes_summary': resolution.get('minutes_summary', ''),
    }
    finding['route'] = route
    finding = apply(finding, DECIDED, f'board resolved; tally {tally}')
    nxt = {policy.ACT_DIRECT: ACTING_DIRECT, policy.DRAFT_FOR_APPROVAL: AWAITING_APPROVAL,
           policy.ESCALATE: ESCALATED}[route]
    return apply(finding, nxt, f'policy route={route}, board motion, conf={confidence:.2f}')


def route_decided(finding, log=print):
    """Safety net: a finding left in Decided is still routed by policy, never dropped."""
    route = finding.get('route') or policy.ESCALATE
    nxt = {policy.ACT_DIRECT: ACTING_DIRECT, policy.DRAFT_FOR_APPROVAL: AWAITING_APPROVAL,
           policy.ESCALATE: ESCALATED}[route]
    return apply(finding, nxt, f'policy route={route} (routed from Decided)')
