"""Summarizer (small model). Evidence refs are validated against the payload; an invalid
ref caps confidence at 0.5 (anti-hallucination guard)."""
import json

from core import router, storage


def _validate_summary(obj, payload):
    assert isinstance(obj.get('confidence'), (int, float)), 'confidence must be a number'
    assert 0 <= float(obj['confidence']) <= 1, 'confidence out of range'
    assert isinstance(obj.get('what_happened'), str) and obj['what_happened'], 'what_happened required'
    ev = obj.get('evidence', [])
    assert isinstance(ev, list), 'evidence must be a list'
    for e in ev:
        assert isinstance(e.get('ref'), str), 'evidence.ref must be a string'


def summarize(finding, log=print):
    payload = finding.get('payload', {})
    past = storage.audit_for(finding['finding_id'])[-5:]
    sys = (
        'You are the summarizer of an autonomous board agent. Reply with JSON only: '
        '{"what_happened": str, "why_it_matters": str, "confidence": 0..1, '
        '"evidence": [{"ref": "payload_key", "quote": str}]}. Every evidence.ref must be a '
        'key that literally exists in the event payload. No prose outside the JSON.'
    )
    user = json.dumps({'event_type': finding.get('event_type'), 'payload': payload,
                       'recent_audit': [a.get('kind') for a in past]})
    resp = router.call_llm('summarize', router.LLMRequest(sys, user), finding_id=finding['finding_id'], ctx={'payload': payload}, log=log)
    try:
        obj = router.extract_json(resp.text, [lambda o: _validate_summary(o, payload)])
    except ValueError as e:
        storage.audit(finding['finding_id'], 'error', {'stage': 'summarize', 'error': str(e)})
        finding['summary'] = {'what_happened': 'Summarizer failed; escalated.', 'confidence': 0.0, 'evidence': [], 'error': str(e)}
        from core.state_machine import apply, ESCALATED
        return apply(finding, ESCALATED, f'summarize failed: {e}')

    bad = [e['ref'] for e in obj.get('evidence', []) if e['ref'] not in payload]
    capped = False
    if bad:
        obj['confidence'] = min(float(obj['confidence']), 0.5)
        capped = True
        storage.audit(finding['finding_id'], 'error', {'stage': 'summarize', 'invalid_evidence_refs': bad, 'confidence_capped': True})
    finding['summary'] = {**obj, 'confidence': float(obj['confidence']), 'evidence_refs_validated': not capped}
    from core.state_machine import apply, SUMMARIZED
    return apply(finding, SUMMARIZED, 'summary written' + (' (confidence capped: bad refs)' if capped else ''))
