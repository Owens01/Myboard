"""Board context + topic policy: the evidence universe the board may cite."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core import context, policy


# ---------- deterministic topic risk (the model never picks risk) ----------

def test_sensitive_topics_fail_closed():
    assert policy.risk_for_topic('our webhook is double-charging customers') == 'sensitive'
    assert policy.risk_for_topic('we found a CVE in mailkit') == 'sensitive'
    assert policy.risk_for_topic('should I give the contractor equity?') == 'sensitive'


def test_low_risk_topics():
    assert policy.risk_for_topic('the deploy is slow, should we add a queue?') == 'low'
    assert policy.risk_for_topic('should we build a roadmap feature next?') == 'low'


def test_unknown_topic_is_sensitive():
    assert policy.category_for_topic('blah blah nothing recognisable') == 'unknown'
    assert policy.risk_for_topic('blah blah nothing recognisable') == 'sensitive'
    assert policy.risk_for_topic('') == 'sensitive'
    assert policy.risk_for_topic(None) == 'sensitive'


def test_mixed_message_stays_sensitive():
    """A cheap technical ask that mentions money must not slip to low risk."""
    assert policy.risk_for_topic('cheaper mailkit upgrade to cut our spend') == 'sensitive'


def test_topic_basis_names_the_matched_keyword():
    category, needle = policy.topic_basis('our refund flow is broken')
    assert category == 'billing' and needle == 'refund'


# ---------- deterministic board ceilings ----------

def test_split_board_cannot_act_confidently():
    ceiling, reason = policy.board_confidence_ceiling({'aye': 2, 'nay': 2, 'abstain': 1}, 0)
    assert ceiling == policy.NO_MANDATE_CONFIDENCE_CAP
    assert 'split board' in reason


def test_a_board_that_voted_against_acting_is_capped():
    ceiling, reason = policy.board_confidence_ceiling({'aye': 0, 'nay': 4}, 0)
    assert ceiling == policy.NO_MANDATE_CONFIDENCE_CAP
    assert 'against acting' in reason


def test_unanimity_in_favour_is_not_capped():
    """Unanimity is not division: four ayes and no nays keeps full confidence."""
    ceiling, reason = policy.board_confidence_ceiling({'aye': 4, 'nay': 0, 'abstain': 1}, 0)
    assert ceiling == 1.0 and reason == ''


def test_ungrounded_evidence_caps_more_severely():
    ceiling, reason = policy.board_confidence_ceiling({'aye': 5, 'nay': 0}, 2)
    assert ceiling == policy.UNGROUNDED_CONFIDENCE_CAP
    assert 'ungrounded' in reason


def test_clean_board_is_not_capped():
    assert policy.board_confidence_ceiling({'aye': 4, 'nay': 0}, 0) == (1.0, '')


# ---------- evidence universe ----------

def _finding(message='our webhook is double-charging customers'):
    return {'finding_id': 'f-test', 'topic_category': policy.category_for_topic(message),
            'risk_tag': policy.risk_for_topic(message), 'payload': {'message': message}}


def test_universe_exposes_real_refs_only():
    u = context.evidence_universe(_finding())
    for ref in ('message', 'founder.said', 'profile.company', 'repo.project',
                'repo.dependencies', 'repo.git', 'policy.risk_tag', 'cloud.facts'):
        assert ref in u, ref
    # empty values are dropped rather than offered as citable evidence
    assert all(v not in ('', '[]', '{}', 'None', 'null') for v in u.values())


def test_universe_carries_real_project_facts():
    u = context.evidence_universe(_finding())
    assert 'python' in u['repo.counts'] or 'python_lines' in u['repo.counts']
    assert 'pytest' in u['repo.dependencies']


def test_risk_tag_derives_from_topic_deterministically():
    low = context.evidence_universe(_finding('we lost a customer over a bug'))
    assert low['policy.risk_tag'] == 'low'  # 'bug' -> product family
    high = context.evidence_universe(_finding('we lost a customer over a duplicate charge'))
    assert high['policy.risk_tag'] == 'sensitive'  # 'charge' -> billing family


def test_ungrounded_refs_are_detected():
    u = context.evidence_universe(_finding())
    bad = context.ungrounded_refs([{'ref': 'message'}, {'ref': 'vibes'}], u)
    assert bad == ['vibes']
    assert context.ungrounded_refs([{'ref': 'message'}], u) == []


def test_render_is_deterministic_and_sorted():
    u = context.evidence_universe(_finding())
    assert context.render(u) == context.render(u)
    assert context.render(u).index('"message"') < context.render(u).index('"repo.project"')


def test_render_can_carry_extra_speaker_context():
    u = context.evidence_universe(_finding())
    out = context.render(u, {'speaker': 'cfo'})
    assert '"speaker": "cfo"' in out
