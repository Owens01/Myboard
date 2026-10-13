"""Deterministic guards that stop the model from acting beyond its evidence.

These are the "policy beats the model" mechanisms: thin evidence caps confidence,
and the operator playbook (not the model) decides what files a fix may touch.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core import policy


def test_confidence_ceiling_absent_data_points_is_no_ceiling():
    assert policy.confidence_ceiling({'title': 'x'}) == (1.0, '')


def test_confidence_ceiling_thin_evidence():
    ceiling, reason = policy.confidence_ceiling({'data_points': 1})
    assert ceiling == 0.45
    assert 'insufficient evidence' in reason


def test_confidence_ceiling_enough_evidence():
    assert policy.confidence_ceiling({'data_points': 3})[0] == 1.0
    assert policy.confidence_ceiling({'data_points': 99})[0] == 1.0


def test_confidence_ceiling_non_numeric_fails_closed():
    ceiling, _ = policy.confidence_ceiling({'data_points': 'a lot'})
    assert ceiling == 0.45


def test_thin_evidence_can_never_reach_act_direct():
    """The whole point: a confident model on thin data still cannot act alone."""
    ceiling, _ = policy.confidence_ceiling({'data_points': 1})
    assert policy.route(0.99, 'low') == policy.ACT_DIRECT          # raw model confidence
    assert policy.route(min(0.99, ceiling), 'low') == policy.ESCALATE  # after our ceiling


def test_scenario_playbook_paths_are_real_and_not_tests():
    import scenarios
    for s in ('A', 'B'):
        brief = scenarios.brief_for(s)
        assert brief['target_paths'], f'scenario {s} must have a playbook'
        for p in brief['target_paths']:
            assert not p.startswith('tests/'), 'the playbook may never point at tests'
            full = os.path.join(os.path.dirname(__file__), '..', 'src', 'demo_company', p)
            assert os.path.exists(full), f'{p} must be a real file in demo_company'


def test_unknown_scenario_has_empty_playbook():
    import scenarios
    assert scenarios.brief_for('Z') == {'goal': '', 'target_paths': []}
    assert scenarios.brief_for(None) == {'goal': '', 'target_paths': []}
