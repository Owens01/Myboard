import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core import policy


def test_risk_by_event_type():
    assert policy.risk_for('dependency_advisory') == 'low'
    assert policy.risk_for('usage_anomaly') == 'low'
    assert policy.risk_for('billing_anomaly') == 'sensitive'

def test_unknown_event_type_fails_closed():
    assert policy.risk_for('nuclear_launch') == 'sensitive'
    assert policy.risk_for('') == 'sensitive'
    assert policy.risk_for(None) == 'sensitive'

def test_route_high_low_risk_acts():
    assert policy.route(0.95, 'low') == policy.ACT_DIRECT

def test_route_medium_low_risk_drafts():
    assert policy.route(0.7, 'low') == policy.DRAFT_FOR_APPROVAL

def test_route_low_confidence_escalates():
    assert policy.route(0.3, 'low') == policy.ESCALATE

def test_sensitive_never_acts_even_at_high_confidence():
    assert policy.route(0.99, 'sensitive') == policy.DRAFT_FOR_APPROVAL

def test_sensitive_low_confidence_escalates():
    assert policy.route(0.3, 'sensitive') == policy.ESCALATE

def test_boundaries():
    assert policy.route(0.85, 'low') == policy.ACT_DIRECT
    assert policy.route(0.60, 'low') == policy.DRAFT_FOR_APPROVAL
    assert policy.route(0.599, 'low') == policy.ESCALATE

def test_override_detection():
    assert policy.check_override('act_direct', policy.DRAFT_FOR_APPROVAL)
    assert policy.check_override('act_direct', policy.ACT_DIRECT) is False
    assert policy.check_override('escalate', policy.ESCALATE) is False
    assert policy.check_override(None, policy.ESCALATE)  # missing suggestion counts as override
