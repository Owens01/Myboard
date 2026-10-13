#!/usr/bin/env python3
"""Live Bedrock smoke test: exercises the real model router (no AWS infra needed).

Run:  python3 scripts/live_smoke.py
Reads BEDROCK_API_KEY from .env. Proves all three tiers answer and that the
strong model returns strict JSON -- before anything is deployed.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

# macOS system Pythons ship no CA bundle; /etc/ssl/cert.pem is present. Lambda has its own.
if 'SSL_CERT_FILE' not in os.environ and os.path.exists('/etc/ssl/cert.pem'):
    os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'

with open(os.path.join(ROOT, '.env')) as fh:
    for line in fh:
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ.setdefault(k, v)

os.environ.setdefault('USE_FAKE_LLM', 'false')

from core import router, storage  # noqa: E402
from core.config import MODEL_CONFIG  # noqa: E402

# Offline: keep audit rows in memory instead of DynamoDB.
ROWS = []
storage.audit = lambda fid, kind, payload, **extra: ROWS.append({'finding_id': fid, 'kind': kind, **extra})
router.storage_audit = lambda fid, stage, resp: ROWS.append(
    {'stage': stage, 'model_id': resp.model_id, 'latency_ms': resp.latency_ms,
     'tokens_in': (resp.usage or {}).get('prompt_tokens', 0),
     'tokens_out': (resp.usage or {}).get('completion_tokens', 0)})

PAYLOAD = {'title': 'mailkit 1.x EOL + CVE-2026-1337', 'package': 'vendor/mailkit',
           'severity': 'high', 'fix': 'upgrade to mailkit 2.x'}
SUMMARIZE_SYS = ('You are the summarizer of an autonomous board agent. Reply with JSON only: '
                 '{"what_happened": str, "why_it_matters": str, "confidence": 0..1, '
                 '"evidence": [{"ref": str, "quote": str}]}. Every evidence.ref must be a key '
                 'that literally exists in the event payload.')
DECIDE_SYS = ('You are the decision agent. Reply with JSON only: {"suggested_action": '
              '"act_direct"|"draft_for_approval"|"escalate", "rationale": str, "confidence": 0..1, '
              '"action_brief": {"goal": str, "target_paths": [str]}}.')


def show(stage, req, validators):
    t0 = time.time()
    resp = router.call_llm(stage, req, finding_id=f'smoke-{stage}')
    try:
        obj = router.extract_json(resp.text, validators)
        ok = 'JSON OK '
    except ValueError as e:
        obj, ok = str(e)[:90], 'JSON BAD'
    print(f"  {stage:10s} tier={MODEL_CONFIG[stage]['tier']:6s} model={resp.model_id:28s} "
          f"{resp.latency_ms:5d}ms {ok} {str(obj)[:130]}")
    return ok == 'JSON OK '


def main():
    print(f"Endpoint: {__import__('core.config', fromlist=['cfg']).cfg()['bedrock_endpoint']}")
    ok = True
    ok &= show('summarize', router.LLMRequest(SUMMARIZE_SYS, __import__('json').dumps(PAYLOAD)),
               [lambda o: isinstance(o.get('confidence'), (int, float))])
    ok &= show('decide', router.LLMRequest(DECIDE_SYS, __import__('json').dumps(
        {'event_type': 'dependency_advisory', 'risk_tag': 'low', 'payload': PAYLOAD})),
        [lambda o: o.get('suggested_action') in ('act_direct', 'draft_for_approval', 'escalate')])
    ok &= show('governance', router.LLMRequest(
        'Reply with JSON only: {"concerns": [str], "verdict": "pass"|"fail"}.',
        __import__('json').dumps({'decision_goal': 'upgrade mailkit to 2.x', 'changed_files': ['app/notifier.py']})),
        [lambda o: 'verdict' in o])
    print('\n--- llm_call audit rows ---')
    for r in ROWS:
        print('  ', {k: v for k, v in r.items() if k in ('stage', 'model_id', 'latency_ms', 'tokens_in', 'tokens_out')})
    print('\nRESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
