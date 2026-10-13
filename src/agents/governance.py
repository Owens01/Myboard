"""Governance: deterministic checks first, cannot be overridden by a model; the small
model may only add concerns. Fail-closed."""
import hashlib
import json
import re

from core import router, storage

MAX_MODEL_CONCERNS = 2
SECRET_PATTERNS = [r'AKIA[0-9A-Z]{16}', r'-----BEGIN [A-Z ]*PRIVATE KEY-----', r'api_key\s*=\s*[\'\"A-Za-z0-9]']
BANNED_IMPORTS = [r'^\s*import\s+(socket|subprocess|requests|http\.client|urllib\.request)\b',
                  r'^\s*from\s+(socket|subprocess|requests|http\.client|urllib\.request)\b',
                  r'os\.system\(']
SENSITIVE_PATH_RE = re.compile(r'^(billing/|.*secret|.*credential|.*comms)')


def _check(finding, sandbox_files, changed, diff_text, approval):
    checks = []
    def add(name, ok, reason=''):
        checks.append({'check': name, 'pass': bool(ok), 'reason': reason})

    attempts = finding.get('attempts') or []
    verification = finding.get('verification') or {}

    if not attempts and verification.get('ran') is False:
        # Nothing was changed, so the patch checks do not apply. Say so explicitly rather
        # than reporting a pass for a change that was never made.
        add('no_code_change_proposed', True, verification.get('reason', 'advice only'))
        add('no_patch_checks_needed', True,
            'no files changed, so scope, secret and import checks are not applicable')
        return checks

    add('verification_ran_green', attempts and attempts[-1].get('passed'),
        'last attempt recorded green' if attempts and attempts[-1].get('passed') else 'no green attempt recorded')
    add('tests_unmodified', attempts and attempts[-1].get('tests_unmodified', False), 'test hashes byte-identical to baseline')
    add('scope_ok', len(changed) <= 4 and all(not p.startswith('tests/') for p in changed), f'changed={changed}')
    add('diff_size_ok', len(diff_text.splitlines()) <= 200, f'{len(diff_text.splitlines())} diff lines')

    sens = any(SENSITIVE_PATH_RE.match(p) for p in changed)
    if sens:
        add('sensitive_paths_have_approval', (approval or {}).get('status') == 'approved',
            f'sensitive changed paths {changed}')
    else:
        add('sensitive_paths_have_approval', True, 'no sensitive paths touched')

    add('no_secrets_in_diff', not any(re.search(p, diff_text) for p in SECRET_PATTERNS), 'secret regex scan')
    add('no_dangerous_imports', not any(re.search(p, diff_text, re.M) for p in BANNED_IMPORTS), 'banned import scan')
    return checks


def run(finding, log=print):
    last = (finding.get('attempts') or [{}])[-1]
    changed = last.get('applied_files') or []
    diff_text = last.get('diff_text', '')
    approval = finding.get('approval') or {}
    checks = _check(finding, None, changed, diff_text, approval)

    # The deterministic checks above are the gate. The small model may only ADD
    # concerns. A model that objects to *the decision* (rather than the diff) is
    # not allowed to block a verified fix, so its concerns are capped and labelled.
    try:
        sys = ('You review a CODE DIFF for a governance checklist. Reply with JSON only: '
               '{"concerns": [str], "verdict": "pass"|"concern"}. Judge only the diff itself: '
               'does it do what the stated goal says, and does it change anything unrelated? '
               'Do NOT question whether the decision should have been made, and do not invent '
               'files that are not in the checklist. Empty concerns means the diff is fine.')
        resp = router.call_llm('governance', router.LLMRequest(sys, json.dumps({
            'goal': ((finding.get('decision') or {}).get('action_brief') or {}).get('goal', ''),
            'checks': checks, 'changed_files': changed, 'diff': diff_text[:4000]})),
            finding_id=finding['finding_id'], log=log)
        obj = json.loads(re.search(r'\{.*\}', resp.text, re.S).group(0))
        for c in (obj.get('concerns') or [])[:MAX_MODEL_CONCERNS]:
            checks.append({'check': 'model_concern', 'pass': False, 'reason': str(c)[:400]})
    except (ValueError, AttributeError, TypeError) as e:
        checks.append({'check': 'model_concern', 'pass': False, 'reason': f'governance model failed: {e}'})

    for c in checks:
        storage.audit(finding['finding_id'], 'governance', {'check': c['check'], 'pass': c['pass'], 'reason': c['reason']})
    ok = all(c['pass'] for c in checks)
    finding['governance'] = {'checks': checks, 'verdict': 'pass' if ok else 'fail'}
    from core.state_machine import apply, CLOSED, ESCALATED, GOVERNANCE_REVIEW, VERIFIYING
    if finding['state'] == VERIFIYING:
        finding = apply(finding, GOVERNANCE_REVIEW, 'entering governance review')
    if ok:
        return apply(finding, CLOSED, 'governance passed')
    return apply(finding, ESCALATED, 'governance failed: ' + '; '.join(c['check'] for c in checks if not c['pass']))
