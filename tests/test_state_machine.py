import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core import state_machine as sm


def make(state='Ingested'):
    return {'finding_id': 'f1', 'state': state, 'version': 1}


def test_happy_path_transitions_exist():
    assert sm.can(sm.INGESTED, sm.SUMMARIZED)
    assert sm.can(sm.DECIDED, sm.ACTING_DIRECT)
    assert sm.can(sm.DECIDED, sm.AWAITING_APPROVAL)
    assert sm.can(sm.DECIDED, sm.ESCALATED)
    assert sm.can(sm.GOVERNANCE_REVIEW, sm.CLOSED)
    assert sm.can(sm.ESCALATED, sm.CLOSED)

def test_illegal_transition_raises():
    import pytest
    with pytest.raises(sm.IllegalTransition):
        sm.apply(make(sm.INGESTED), sm.CLOSED)
    with pytest.raises(sm.IllegalTransition):
        sm.apply(make(sm.CLOSED), sm.INGESTED)

def test_apply_persists_with_version_bump(monkeypatch):
    calls = []
    monkeypatch.setattr(sm.storage, 'audit', lambda *a, **k: calls.append(('audit', a[1])))
    monkeypatch.setattr(sm.storage, 'put_finding', lambda f, expect_version=None: calls.append(('put', expect_version)))
    f = make()
    sm.apply(f, sm.SUMMARIZED, 'test')
    assert f['version'] == 2 and f['state'] == sm.SUMMARIZED
    assert calls[0] == ('audit', 'transition')  # audit before persist
    assert calls[1] == ('put', 1)

def test_orchestrator_runs_to_parked(monkeypatch):
    monkeypatch.setattr(sm.storage, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(sm.storage, 'put_finding', lambda f, expect_version=None: f)
    f = make()
    f['state'] = sm.INGESTED
    handlers = {
        sm.INGESTED: lambda x: sm.apply(x, sm.SUMMARIZED, ''),
        sm.SUMMARIZED: lambda x: sm.apply(x, sm.DECIDED, ''),
        sm.DECIDED: lambda x: sm.apply(x, sm.ESCALATED, ''),
    }
    sm.Orchestrator(handlers).run(f)
    assert f['state'] == sm.ESCALATED  # parked: Escalated handler is NOT auto-run

def test_orchestrator_idempotent_park(monkeypatch):
    monkeypatch.setattr(sm.storage, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(sm.storage, 'put_finding', lambda f, expect_version=None: f)
    f = make()
    f['state'] = sm.AWAITING_APPROVAL
    before = f['state']
    sm.Orchestrator({}).run(f)
    assert f['state'] == before  # parked: nothing executed
