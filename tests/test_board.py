"""Board room: deliberation, deterministic ceilings, idempotency, fail-closed routing."""
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from core import state_machine as sm
from core import policy
from agents import board


class FakeStore:
    def __init__(self):
        self.findings, self.audit_rows = {}, []

    def audit(self, fid, kind, payload, **extra):
        item = {'finding_id': fid or '__system__', 'kind': kind, 'payload': payload, **extra}
        self.audit_rows.append(item)
        return item

    def audit_for(self, fid):
        return [a for a in self.audit_rows if a['finding_id'] == fid]

    def create_finding(self, f):
        self.findings[f['finding_id']] = dict(f)
        return f

    def get_finding(self, fid):
        item = self.findings.get(fid)
        return dict(item) if item else None

    def put_finding(self, f, expect_version=None):
        self.findings[f['finding_id']] = dict(f)
        return f

    def list_findings(self, limit=50):
        return []


@pytest.fixture
def store(monkeypatch):
    fs = FakeStore()
    from core import storage
    for name in ('audit', 'audit_for', 'create_finding', 'get_finding', 'put_finding', 'list_findings'):
        monkeypatch.setattr(storage, name, getattr(fs, name))
    return fs


DEFAULT_SCRIPT = {
    'votes': {'board_cto': 'aye', 'board_cfo': 'aye', 'board_risk': 'aye', 'board_growth': 'aye'},
    'confidence': 0.9,
    'evidence': [{'ref': 'message', 'quote': 'the founder said so'}],
    'chair_confidence': 0.9,
    'chair_action': 'act_direct',
    'paths': [],
}


def script_llm(monkeypatch, **overrides):
    """Scripted board: lets each test drive votes, confidence and citations."""
    from core import router
    script = dict(DEFAULT_SCRIPT, **overrides)
    script['calls'] = []

    def fake_call(stage, request, finding_id=None, ctx=None, log=print):
        script['calls'].append(stage)
        if stage in ('board_cto', 'board_cfo', 'board_risk', 'board_growth'):
            body = {
                'position': f'{stage} position',
                'reasoning': 'reasoning',
                'evidence': script['evidence'],
                'confidence': script['confidence'],
                'vote': script['votes'].get(stage, 'aye'),
                'concerns': ['c'],
                'recommends_action': True,
            }
        elif stage == 'board_chair':
            body = {
                'motion': 'proceed',
                'recommendation': 'the board advises proceeding',
                'consensus': ['agreed'],
                'dissent': ['risk seat objected'],
                'confidence': script['chair_confidence'],
                'suggested_action': script['chair_action'],
                'action_brief': {'goal': 'g', 'target_paths': script['paths']},
                'minutes_summary': 'minutes',
            }
        else:
            body = {}
        return router.LLMResponse(json.dumps(body), 'fake-model',
                                  {'prompt_tokens': 10, 'completion_tokens': 5}, 1, 'stop')

    monkeypatch.setattr(router, 'call_llm', fake_call)
    monkeypatch.setattr(router, 'storage_audit', lambda *a, **k: None)
    return script


def board_finding(message, state=sm.SUMMARIZED, scenario=''):
    return {
        'finding_id': 'f-board', 'state': state, 'version': 1, 'mode': 'board',
        'event_type': 'founder_message',
        'topic_category': policy.category_for_topic(message),
        'risk_tag': policy.risk_for_topic(message),
        'meeting_id': 'm-test', 'thread_id': 'm-test', 'scenario': scenario,
        'payload': {'message': message, 'round': 1},
        'source': 'founder', 'summary': {'what_happened': 'x', 'confidence': 0.9},
        'decision': None, 'route': None, 'attempt_count': 0, 'created_at': 0,
    }


def run(finding, store):
    store.create_finding(finding)
    return board.convene(finding)


# ---------- the session runs ----------

def test_all_seats_speak_and_a_tally_is_counted(store, monkeypatch):
    script_llm(monkeypatch)
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    turns = [a for a in store.audit_for('f-board') if a['kind'] == 'board_turn']
    assert [t['payload']['role'] for t in turns] == [s['role'] for s in board.BOARD]
    assert got['board']['tally']['aye'] == 4
    assert got['board']['tally']['cast'] == 4


def test_deliberating_state_is_visited(store, monkeypatch):
    script_llm(monkeypatch)
    run(board_finding('the deploy feels slow, should we add a queue?'), store)
    states = [a['payload'].get('to') for a in store.audit_for('f-board') if a['kind'] == 'transition']
    assert sm.DELIBERATING in states
    assert states[-1] in (sm.ACTING_DIRECT, sm.AWAITING_APPROVAL, sm.ESCALATED)


def test_each_seat_sees_the_transcript_so_far(store, monkeypatch):
    from core import router
    transcripts = []

    def spy(stage, request, finding_id=None, ctx=None, log=print):
        if stage in ('board_cto', 'board_cfo', 'board_risk', 'board_growth'):
            transcripts.append(request.user)
            body = {'position': 'p', 'reasoning': 'r',
                    'evidence': [{'ref': 'message', 'quote': 'q'}],
                    'confidence': 0.9, 'vote': 'aye', 'concerns': [], 'recommends_action': True}
        elif stage == 'board_chair':
            body = {'motion': 'm', 'recommendation': 'r', 'consensus': [], 'dissent': [],
                    'confidence': 0.9, 'suggested_action': 'escalate',
                    'action_brief': {'goal': 'g', 'target_paths': []}, 'minutes_summary': 's'}
        else:
            body = {}
        return router.LLMResponse(json.dumps(body), 'fake-model', {}, 1, 'stop')

    monkeypatch.setattr(router, 'call_llm', spy)
    run(board_finding('the deploy feels slow, should we add a queue?'), store)
    assert len(transcripts) == 4
    # by the third speaker, an earlier position is already in the transcript
    assert 'position' in transcripts[2]


# ---------- deterministic ceilings ----------

def test_ungrounded_citations_cap_the_speaker_and_the_resolution(store, monkeypatch):
    script_llm(monkeypatch, evidence=[{'ref': 'vibes', 'quote': 'made up'}],
               chair_confidence=0.99, chair_action='act_direct')
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    turns = [a['payload'] for a in store.audit_for('f-board') if a['kind'] == 'board_turn']
    assert all(t['confidence'] == 0.5 for t in turns)
    assert all(t['ungrounded_refs'] == ['vibes'] for t in turns)
    guards = [a for a in store.audit_for('f-board')
              if a['kind'] == 'constraint' and a['payload'].get('guard') == 'grounded_evidence']
    assert len(guards) == 4
    assert got['decision']['confidence'] == policy.UNGROUNDED_CONFIDENCE_CAP
    assert got['route'] == policy.DRAFT_FOR_APPROVAL  # 0.70 is only medium confidence


def test_split_board_cannot_act_confidently(store, monkeypatch):
    script_llm(monkeypatch, votes={'board_cto': 'nay', 'board_cfo': 'nay',
                                   'board_risk': 'aye', 'board_growth': 'aye'},
               chair_confidence=0.99, chair_action='act_direct')
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    assert got['decision']['confidence'] == policy.NO_MANDATE_CONFIDENCE_CAP
    # below the medium threshold on purpose: a board with no mandate does not get to act,
    # and does not get to hand the founder a draft to rubber-stamp either
    assert got['route'] == policy.ESCALATE
    assert got['state'] == sm.ESCALATED
    ceilings = [a for a in store.audit_for('f-board')
                if a['kind'] == 'constraint' and a['payload'].get('guard') == 'board_ceiling']
    assert ceilings and 'split board' in ceilings[0]['payload']['reason']
    overrides = [a for a in store.audit_for('f-board') if a['kind'] == 'policy_override']
    assert overrides, 'the model proposed acting; policy must record the override'


def test_unanimous_agreement_is_not_capped(store, monkeypatch):
    script_llm(monkeypatch, chair_confidence=0.99, chair_action='act_direct')
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    assert got['decision']['confidence'] == 0.99
    assert got['route'] == policy.ACT_DIRECT


def test_scope_guard_drops_paths_that_do_not_exist(store, monkeypatch):
    script_llm(monkeypatch, paths=['src/core/policy.py', 'made/up/file.py'])
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    assert got['decision']['action_brief']['target_paths'] == ['src/core/policy.py']
    scope = [a for a in store.audit_for('f-board')
             if a['kind'] == 'constraint' and a['payload'].get('guard') == 'existing_paths_only']
    assert scope and scope[0]['payload']['dropped'] == ['made/up/file.py']


# ---------- deterministic handoff to a real code playbook ----------

def test_playbook_detection_is_deterministic():
    import scenarios
    assert scenarios.playbook_for('our webhook retries are double-charging clinics') == 'B'
    assert scenarios.playbook_for('we should upgrade mailkit, it is end of life') == 'A'
    assert scenarios.playbook_for('should we hire a designer?') == ''
    assert scenarios.playbook_for('') == ''


def test_monitor_assigns_a_code_playbook_and_the_risk(store):
    from agents import monitor
    f = monitor.make_finding({'event_id': 'e1', 'source': 'founder', 'meeting_id': 'm-1',
                              'payload': {'message': 'the webhook retries double-charge customers'}})
    assert f['mode'] == 'board'
    assert f['scenario'] == 'B'          # executable and verifiable
    assert f['risk_tag'] == 'sensitive'  # and still needs approval
    f2 = monitor.make_finding({'event_id': 'e2', 'source': 'founder', 'meeting_id': 'm-2',
                               'payload': {'message': 'should we raise our prices next quarter?'}})
    assert f2['scenario'] == ''
    assert f2['risk_tag'] == 'sensitive'


def test_operator_playbook_owns_the_blast_radius(store, monkeypatch):
    """For a code topic the model may not choose target_paths, even if it tries."""
    script_llm(monkeypatch, paths=['some/other/place.py'])
    got = run(board_finding('our webhook retries are double-charging clinics',
                            scenario='B'), store)
    assert got['decision']['action_brief']['target_paths'] == ['billing/webhooks.py']
    scopes = [a for a in store.audit_for('f-board')
              if a['kind'] == 'constraint' and a['payload'].get('guard') == 'playbook_scope']
    assert scopes and scopes[0]['payload']['model_proposed_paths'] == ['some/other/place.py']


# ---------- advice is not reported as verified work ----------

def test_advice_only_session_is_not_reported_as_green(store):
    from agents import action, governance
    f = board_finding('should we hire a designer?', state=sm.ACTING_DIRECT)
    f['decision'] = {'action_brief': {'goal': 'advise', 'target_paths': []}}
    store.create_finding(f)
    f = action.act(f)
    assert f['state'] == sm.VERIFIYING
    assert f['verification']['ran'] is False
    assert f['verification']['advice_only'] is True
    assert not (f.get('attempts') or []), 'no attempt may be recorded when nothing ran'
    f = governance.run(f)
    assert f['state'] == sm.CLOSED
    checks = {c['check']: c['pass'] for c in f['governance']['checks']}
    assert checks.get('no_code_change_proposed') is True
    assert 'verification_ran_green' not in checks, 'a change that never happened is not green'


# ---------- idempotency ----------

def test_a_resumed_session_does_not_repeat_a_speaker(store, monkeypatch):
    """A crash mid-deliberation must resume, not restart: the CTO does not speak twice."""
    script = script_llm(monkeypatch)
    f = board_finding('the deploy feels slow, should we add a queue?', state=sm.DELIBERATING)
    store.create_finding(f)
    store.audit('f-board', 'board_turn', {'key': 'cto', 'role': 'CTO', 'vote': 'aye',
                                          'model_id': 'fake-model', 'confidence': 0.9})
    got = board.convene(f)
    assert 'board_cto' not in script['calls']
    assert set(script['calls']) == {'board_cfo', 'board_risk', 'board_growth', 'board_chair'}
    assert len([a for a in store.audit_for('f-board') if a['kind'] == 'board_turn']) == 4
    assert got['board']['tally']['cast'] == 4


def test_convene_is_a_noop_once_the_session_has_moved_on(store, monkeypatch):
    script = script_llm(monkeypatch)
    f = board_finding('the deploy feels slow', state=sm.ACTING_DIRECT)
    store.create_finding(f)
    got = board.convene(f)
    assert got['state'] == sm.ACTING_DIRECT
    assert script['calls'] == []


def test_a_member_that_breaks_fails_closed_not_silently(store, monkeypatch):
    from core import router

    def broken(stage, request, finding_id=None, ctx=None, log=print):
        if stage == 'board_cfo':
            return router.LLMResponse('not json at all', 'fake-model', {}, 1, 'stop')
        return router.LLMResponse(json.dumps({
            'position': 'p', 'reasoning': 'r',
            'evidence': [{'ref': 'message', 'quote': 'q'}],
            'confidence': 0.9, 'vote': 'aye', 'concerns': [], 'recommends_action': True,
        }), 'fake-model', {}, 1, 'stop')

    monkeypatch.setattr(router, 'call_llm', broken)
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    assert got['state'] == sm.ESCALATED
    errors = [a for a in store.audit_for('f-board') if a['kind'] == 'error']
    assert errors and errors[0]['payload']['role'] == 'CFO'


# ---------- state machine + helpers ----------

def test_deliberating_transitions_are_legal_and_illegal_ones_raise():
    assert sm.can(sm.SUMMARIZED, sm.DELIBERATING)
    assert sm.can(sm.DELIBERATING, sm.DECIDED)
    assert sm.can(sm.DELIBERATING, sm.ESCALATED)
    assert not sm.can(sm.DELIBERATING, sm.ACTING_DIRECT)
    assert not sm.can(sm.INGESTED, sm.DELIBERATING)


def test_route_decided_is_a_safety_net(store):
    f = board_finding('the deploy feels slow')
    f['state'] = sm.DECIDED
    f['route'] = policy.ESCALATE
    store.create_finding(f)
    got = board.route_decided(f)
    assert got['state'] == sm.ESCALATED
    assert store.audit_for('f-board')[-1]['kind'] == 'transition'


def test_tally_counts_only_real_votes():
    turns = [{'vote': 'aye'}, {'vote': 'nay'}, {'vote': 'aye'}, {'vote': 'nonsense'}]
    assert board.tally_votes(turns) == {'aye': 2, 'nay': 1, 'conditional': 0,
                                        'abstain': 0, 'cast': 4}


def test_member_validator_rejects_bad_shapes():
    bad_shapes = [{}, {'position': 'p'},
                  {'position': 'p', 'reasoning': 'r', 'confidence': 2, 'vote': 'aye'},
                  {'position': 'p', 'reasoning': 'r', 'confidence': 0.5, 'vote': 'maybe'}]
    for bad in bad_shapes:
        with pytest.raises(AssertionError):
            board.validate_member(bad)


def test_chair_validator_rejects_bad_shapes():
    bad_shapes = [{}, {'motion': 'm'},
                  {'motion': 'm', 'recommendation': 'r', 'confidence': 0.5,
                   'suggested_action': 'shrug'}]
    for bad in bad_shapes:
        with pytest.raises(AssertionError):
            board.validate_chair(bad)


def test_every_board_seat_has_its_own_configured_model():
    from core.config import MODEL_CONFIG
    models = set()
    for seat in board.BOARD + [board.CHAIR]:
        model = MODEL_CONFIG[seat['stage']]['model_id']
        assert model, seat['stage']
        models.add(model)
    assert len(models) >= 4, f'seats should route across families, got {models}'


def test_prompts_are_present_and_versioned():
    member = board.load_prompt(board.MEMBER_PROMPT)
    chair = board.load_prompt(board.CHAIR_PROMPT)
    assert '{{ROLE}}' in member and '{{CONTEXT}}' in member
    assert '{{TALLY}}' in chair and '{{TRANSCRIPT}}' in chair


def test_sensitive_topic_cannot_be_acted_on_autonomously(store, monkeypatch):
    script_llm(monkeypatch, chair_action='act_direct', chair_confidence=0.99)
    got = run(board_finding('our webhook retries are double-charging customers'), store)
    assert got['route'] == policy.DRAFT_FOR_APPROVAL
    assert got['state'] == sm.AWAITING_APPROVAL
    overrides = [a for a in store.audit_for('f-board') if a['kind'] == 'policy_override']
    assert overrides, 'policy must overrule a confident board on a sensitive topic'
    assert overrides[0]['payload']['source'] == 'board'


def test_low_risk_confident_board_may_act(store, monkeypatch):
    script_llm(monkeypatch, chair_action='act_direct', chair_confidence=0.9)
    got = run(board_finding('the deploy feels slow, should we add a queue?'), store)
    assert got['route'] == policy.ACT_DIRECT


def test_unknown_topic_fails_closed_to_approval(store, monkeypatch):
    script_llm(monkeypatch, chair_action='act_direct', chair_confidence=0.99)
    got = run(board_finding('something odd happened and I cannot tell you what'), store)
    assert got['risk_tag'] == 'sensitive'
    assert got['route'] == policy.DRAFT_FOR_APPROVAL
