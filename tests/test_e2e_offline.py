"""Full pipeline offline: FakeLLM + fake storage + REAL sandbox pytest runs.
Validates Ingested -> ... -> Closed for scenarios A, B and C without AWS."""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from core import state_machine as sm
from agents import action as action_mod


class FakeStore:
    def __init__(self):
        self.findings, self.audit_rows, self.feed, self.artifacts = {}, [], {}, {}

    def audit(self, fid, kind, payload, **extra):
        item = {'finding_id': fid or '__system__', 'ts_seq': f'{len(self.audit_rows)}', 'kind': kind, 'payload': payload, **extra}
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
        if expect_version is not None and self.findings[f['finding_id']].get('version') != expect_version:
            raise AssertionError('version conflict')
        self.findings[f['finding_id']] = dict(f)
        return f

    def put_artifact(self, key, content):
        self.artifacts[key] = content
        return key

    def is_paused(self):
        return False


def _bucket_stub():
    class B:
        def Object(self, k):
            class O:
                def get(self):
                    raise FileNotFoundError
            return O()
    return B()


@pytest.fixture
def fake_store(monkeypatch):
    fs = FakeStore()
    from core import storage
    for name in ('audit', 'audit_for', 'create_finding', 'get_finding', 'put_finding', 'put_artifact', 'is_paused'):
        monkeypatch.setattr(storage, name, getattr(fs, name))
    monkeypatch.setattr(storage, '_bucket', _bucket_stub)
    return fs


def make_finding(scenario, event_type, risk, payload, suggested='act_direct', conf=0.9):
    return {'finding_id': f'test-{scenario}', 'state': sm.INGESTED, 'version': 1, 'scenario': scenario,
            'event_type': event_type, 'risk_tag': risk, 'payload': payload, 'source': 'fixture',
            'summary': None, 'decision': None, 'route': None, 'attempt_count': 0, 'created_at': 0}


def script_llm(monkeypatch, scenario):
    from core import router
    def fake_call(stage, request, finding_id=None, ctx=None, log=print):
        if stage == 'summarize':
            payload = (ctx or {}).get('payload') or {}
            ref = next(iter(payload))
            text = json.dumps({'what_happened': 'x', 'why_it_matters': 'y', 'confidence': 0.9,
                               'evidence': [{'ref': ref, 'quote': 'q'}]})
        elif stage == 'decide':
            risk = (ctx or {}).get('risk_tag', 'low')
            conf = 0.9 if scenario in ('A', 'B') else 0.3
            text = json.dumps({'suggested_action': suggested_action(scenario), 'rationale': 'r',
                               'confidence': conf, 'action_brief': dict(scen_brief(scenario))})
        elif stage == 'code':
            text = json.dumps({'files': code_files(scenario), 'explanation': 'fix applied'})
        else:
            text = json.dumps({'concerns': [], 'verdict': 'pass'})
        return router.LLMResponse(text, 'fake-model', {'inputTokens': 1, 'outputTokens': 1}, 1, 'end_turn')
    monkeypatch.setattr(router, 'call_llm', fake_call)


def suggested_action(scenario):
    return 'act_direct' if scenario == 'A' else ('act_direct' if scenario == 'B' else 'escalate')


def scen_brief(scenario):
    from scenarios import BRIEFS
    return BRIEFS[scenario]


def code_files(scenario):
    if scenario == 'A':
        return [
            {'path': 'app/notifier.py', 'new_content': (
                'from vendor.mailkit import send_message\n\n\ndef notify(to, subject, body, reply_to=None):\n'
                '    result = send_message(recipient=to, subject=subject, body=body, reply_to=reply_to)\n'
                '    return result\n')},
            {'path': 'app/reminders.py', 'new_content': (
                'from vendor.mailkit import send_message\n\n\ndef send_reminder(to, text, reply_to=None):\n'
                '    return send_message(recipient=to, subject="Payment reminder", body=text, reply_to=reply_to)\n')},
        ]
    if scenario == 'B':
        return [{'path': 'billing/webhooks.py', 'new_content': (
            'from billing import charges\n\n\n_PROCESSED = set()\n\n\ndef reset():\n'
            '    _PROCESSED.clear()\n\n\ndef handle_payment(event):\n'
            '    eid = event[\'id\']\n    if eid in _PROCESSED:\n'
            '        return {\'processed\': False, \'event_id\': eid}\n'
            '    _PROCESSED.add(eid)\n    charges.charge(event[\'customer\'], event[\'amount\'])\n'
            '    return {\'processed\': True, \'event_id\': eid}\n')}]
    return []


def run_pipeline(fid, fs, monkeypatch):
    from worker_handler import _handlers
    finding = fs.get_finding(fid)
    sm.Orchestrator(_handlers(finding)).run(finding)
    return fs.findings[fid]


def test_scenario_a_end_to_end_goes_green(fake_store, monkeypatch):
    script_llm(monkeypatch, 'A')
    f = make_finding('A', 'dependency_advisory', 'low', {'title': 'mailkit EOL'})
    fake_store.create_finding(f)
    run_pipeline('test-A', fake_store, monkeypatch)
    f = fake_store.findings['test-A']
    assert f['state'] == sm.CLOSED, f.get('governance')
    kinds = [a['kind'] for a in fake_store.audit_for(f['finding_id'])]
    assert 'attempt' in kinds and 'governance' in kinds
    # the model patch must have really been applied: no policy override expected on A
    assert not [a for a in fake_store.audit_for(f['finding_id']) if a['kind'] == 'policy_override']


def test_scenario_b_policy_forces_approval_then_closes(fake_store, monkeypatch):
    script_llm(monkeypatch, 'B')
    f = make_finding('B', 'billing_anomaly', 'sensitive', {'title': 'dupes'})
    fake_store.create_finding(f)
    run_pipeline('test-B', fake_store, monkeypatch)
    f = fake_store.findings['test-B']
    assert f['state'] == sm.AWAITING_APPROVAL, f.get('route')
    assert f['route'] == 'DRAFT_FOR_APPROVAL'
    overrides = [a for a in fake_store.audit_for(f['finding_id']) if a['kind'] == 'policy_override']
    assert overrides, 'policy must have overruled act_direct on a sensitive event'
    from worker_handler import resume
    resume('test-B', True)
    from worker_handler import orchestrate
    orchestrate('test-B')
    assert fake_store.findings['test-B']['state'] == sm.CLOSED
    assert fake_store.findings['test-B']['approval']['status'] == 'approved'


def test_scenario_c_escalates(fake_store, monkeypatch):
    script_llm(monkeypatch, 'C')
    f = make_finding('C', 'usage_anomaly', 'low', {'title': 'dip'})
    fake_store.create_finding(f)
    run_pipeline('test-C', fake_store, monkeypatch)
    assert fake_store.findings['test-C']['state'] == sm.ESCALATED  # parked for the human
    assert fake_store.findings['test-C']['route'] == 'ESCALATE'
    from worker_handler import resume
    resume('test-C', True)  # human resolves: no action taken
    assert fake_store.findings['test-C']['state'] == sm.CLOSED


def test_retry_after_red_attempt_recovers_and_closes(fake_store, monkeypatch):
    """Regression: a failed attempt must loop back through Verifying -> ActingDirect
    (previously raised IllegalTransition: ActingDirect -> ActingDirect), then pass."""
    from core import router
    calls = {'n': 0}

    def fake_call(stage, request, finding_id=None, ctx=None, log=print):
        if stage == 'summarize':
            payload = (ctx or {}).get('payload') or {}
            text = json.dumps({'what_happened': 'x', 'why_it_matters': 'y', 'confidence': 0.9,
                               'evidence': [{'ref': next(iter(payload)), 'quote': 'q'}]})
        elif stage == 'decide':
            text = json.dumps({'suggested_action': 'act_direct', 'rationale': 'r', 'confidence': 0.9,
                               'action_brief': dict(scen_brief('A'))})
        elif stage == 'code':
            calls['n'] += 1
            if calls['n'] == 1:
                # lazy patch: only one call site fixed, return type mishandled
                files = [{'path': 'app/notifier.py', 'new_content':
                          'from vendor.mailkit import send_message\n\n\ndef notify(to, subject, body):\n'
                          '    return send_message(recipient=to, subject=subject, body=body)\n'}]
            else:
                files = code_files('A')
            text = json.dumps({'files': files, 'explanation': 'attempt %d' % calls['n']})
        else:
            text = json.dumps({'concerns': [], 'verdict': 'pass'})
        return router.LLMResponse(text, 'fake-model', {'inputTokens': 1, 'outputTokens': 1}, 1, 'end_turn')

    monkeypatch.setattr(router, 'call_llm', fake_call)
    f = make_finding('A', 'dependency_advisory', 'low', {'title': 'mailkit EOL'})
    f['finding_id'] = 'test-retry'
    fake_store.create_finding(f)
    run_pipeline('test-retry', fake_store, monkeypatch)

    f = fake_store.findings['test-retry']
    assert f['state'] == sm.CLOSED, f.get('governance')
    assert f['attempt_count'] == 2, 'must have taken exactly two attempts'
    attempts = [a for a in fake_store.audit_for(f['finding_id']) if a['kind'] == 'attempt']
    assert len(attempts) == 2
    assert attempts[0]['payload']['result']['passed'] is False
    assert attempts[1]['payload']['result']['passed'] is True
    # Verifying must have been visited on the retry loop (the transition that used to raise)
    states = [a['payload']['to'] for a in fake_store.audit_for(f['finding_id']) if a['kind'] == 'transition']
    assert states.count(sm.VERIFIYING) >= 2
    assert states.count(sm.ACTING_DIRECT) >= 2


def test_real_pytest_baseline_is_red_after_setup(monkeypatch, tmp_path):
    """The scenario A setup step genuinely breaks the sandbox: baseline pytest is red."""
    import shutil, scenarios
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'demo_company'))
    sandbox = tmp_path / 'sb'
    shutil.copytree(src, sandbox)
    scenarios.setup(str(sandbox), 'A')
    from agents.action import _run_pytest
    result = _run_pytest(str(sandbox))
    assert result['exit_code'] != 0, 'baseline must be red: app code still uses mailkit 1.x API'


def test_correct_patch_makes_sandbox_green(monkeypatch, tmp_path):
    """A correct patch (the one a good model returns) makes the same sandbox green."""
    import shutil, scenarios
    from agents.action import _run_pytest, _apply_patch
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'demo_company'))
    sandbox = tmp_path / 'sb'
    shutil.copytree(src, sandbox)
    scenarios.setup(str(sandbox), 'A')
    text = json.dumps({'files': code_files('A'), 'explanation': 'x'})
    applied = _apply_patch(str(sandbox), text, ['app/notifier.py', 'app/reminders.py'], print)
    assert applied == ['app/notifier.py', 'app/reminders.py']
    result = _run_pytest(str(sandbox))
    assert result['exit_code'] == 0, result['output_tail']


def test_lazy_patch_still_fails(monkeypatch, tmp_path):
    """A lazy patch (one call site fixed, bool mishandled) must NOT go green."""
    import shutil, scenarios
    from agents.action import _run_pytest, _apply_patch
    src = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'demo_company'))
    sandbox = tmp_path / 'sb'
    shutil.copytree(src, sandbox)
    scenarios.setup(str(sandbox), 'A')
    lazy = [{'path': 'app/notifier.py', 'new_content': 'from vendor.mailkit import send_message\n\ndef notify(to, subject, body):\n    return send_message(recipient=to, subject=subject, body=body)\n'}]
    _apply_patch(str(sandbox), json.dumps({'files': lazy}), ['app/notifier.py'], print)
    result = _run_pytest(str(sandbox))
    assert result['exit_code'] != 0, 'lazy patch must fail (reminders.py untouched)'
