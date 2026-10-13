"""Decision agent (strong model). The model only suggests; the policy engine decides."""
import json

from core import router, storage
from core import policy


def _validate(obj):
    assert obj.get('suggested_action') in ('act_direct', 'draft_for_approval', 'escalate'), 'bad suggested_action'
    assert isinstance(obj.get('confidence'), (int, float)) and 0 <= float(obj['confidence']) <= 1
    assert isinstance(obj.get('rationale'), str) and obj['rationale']
    ab = obj.get('action_brief') or {}
    assert isinstance(ab.get('goal'), str)
    assert isinstance(ab.get('target_paths'), list)


def _playbook(finding):
    """Operator runbook for this scenario, or {} when there is none."""
    try:
        import scenarios as scen
        return scen.brief_for(finding.get('scenario'))
    except Exception:
        return {}


def decide(finding, log=print):
    from core.state_machine import apply, DECIDED, SUMMARIZED
    if finding['state'] == SUMMARIZED:
        finding = apply(finding, DECIDED, 'decision starting')  # pass through Decided
    sys = (
        'You are the decision agent of an autonomous board agent. Reply with JSON only: '
        '{"suggested_action": "act_direct"|"draft_for_approval"|"escalate", "rationale": str, '
        '"confidence": 0..1, "action_brief": {"goal": str, "target_paths": [str]}}. '
        'The policy engine will make the final call; you only suggest.'
    )
    user = json.dumps({
        'event_type': finding.get('event_type'), 'risk_tag': finding.get('risk_tag'),
        'summary': finding.get('summary'), 'payload': finding.get('payload', {}),
        # The operator's known remediation runbook for this event type, when one
        # exists. It describes *how* the fix is done, never *whether* to act.
        'known_remediation_playbook': (_playbook(finding) or None),
    })
    resp = router.call_llm('decide', router.LLMRequest(sys, user), finding_id=finding['finding_id'],
                           ctx={'risk_tag': finding.get('risk_tag')}, log=log)
    try:
        obj = router.extract_json(resp.text, [_validate])
    except ValueError as e:
        storage.audit(finding['finding_id'], 'error', {'stage': 'decide', 'error': str(e)})
        obj = {'suggested_action': 'escalate', 'rationale': f'decision parse failed: {e}', 'confidence': 0.0, 'action_brief': {'goal': '', 'target_paths': []}}

    conf = float(obj['confidence'])
    fid = finding['finding_id']

    # (1) Deterministic evidence-sufficiency ceiling: the model's stated
    #     confidence is capped by our own check before it can influence routing.
    ceiling, reason = policy.confidence_ceiling(finding.get('payload'))
    if conf > ceiling:
        storage.audit(fid, 'constraint', {'stage': 'decide', 'guard': 'evidence_sufficiency',
                                          'model_confidence': conf, 'applied_confidence': ceiling,
                                          'reason': reason})
        conf = ceiling

    # (2) The remediation playbook (operator-authored, per scenario) constrains the
    #     blast radius. The model may not invent paths or drift outside it.
    brief = dict(obj.get('action_brief') or {})
    import scenarios as scen
    playbook = scen.brief_for(finding.get('scenario'))
    if playbook['target_paths']:
        proposed = list(brief.get('target_paths') or [])
        if proposed != playbook['target_paths']:
            storage.audit(fid, 'constraint', {'stage': 'decide', 'guard': 'playbook_scope',
                                              'model_proposed_paths': proposed,
                                              'applied_paths': playbook['target_paths'],
                                              'reason': 'model proposed paths outside the operator playbook'})
        brief = {'goal': playbook['goal'], 'target_paths': playbook['target_paths']}

    pr = policy.route(conf, finding['risk_tag'])
    overrode = policy.check_override(obj['suggested_action'], pr)
    finding['decision'] = {**obj, 'confidence': conf, 'action_brief': brief, 'model_confidence': float(obj['confidence'])}
    finding['route'] = pr
    if overrode:
        storage.audit(fid, 'policy_override',
                      {'model_suggested': obj['suggested_action'], 'policy_route': pr,
                       'risk_tag': finding['risk_tag'], 'confidence': conf})
    from core.state_machine import apply, ACTING_DIRECT, AWAITING_APPROVAL, ESCALATED, DECIDED
    nxt = {policy.ACT_DIRECT: ACTING_DIRECT, policy.DRAFT_FOR_APPROVAL: AWAITING_APPROVAL, policy.ESCALATE: ESCALATED}[pr]
    return apply(finding, nxt, f'policy route={pr}, model={obj["suggested_action"]}, conf={conf:.2f}')
