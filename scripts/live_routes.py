#!/usr/bin/env python3
"""Live route-stability probe: runs the REAL summarizer + decision agents against
Bedrock N times per scenario and reports the policy route distribution.

This is how we validated that the demo is deterministic enough to record.
Run:  python3 scripts/live_routes.py [runs]
"""
import collections
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src'))

if 'SSL_CERT_FILE' not in os.environ and os.path.exists('/etc/ssl/cert.pem'):
    os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'
with open(os.path.join(ROOT, '.env')) as fh:
    for line in fh:
        if '=' in line and not line.strip().startswith('#'):
            k, v = line.strip().split('=', 1)
            os.environ.setdefault(k, v)
os.environ['USE_FAKE_LLM'] = 'false'

from core import router, storage, policy  # noqa: E402
from agents import summarizer, decision  # noqa: E402

ROWS = []
storage.audit = lambda fid, kind, payload, **extra: ROWS.append({'kind': kind, 'payload': payload, **extra})
storage.audit_for = lambda fid: []
storage.put_finding = lambda f, expect_version=None: f
router.storage_audit = lambda *a, **k: None

sys.path.insert(0, os.path.join(ROOT, 'src'))
import scenarios  # noqa: E402

EVENTS = {
    'A': ('dependency_advisory', {'title': 'mailkit 1.x EOL + CVE-2026-1337', 'package': 'vendor/mailkit',
                                  'severity': 'high', 'fix': 'upgrade to mailkit 2.x', 'cve': 'CVE-2026-1337'}),
    'B': ('billing_anomaly', {'title': 'Duplicate charges spike', 'customers_affected': 3,
                              'pattern': 'same customer+amount within 90s',
                              'suspected_cause': 'webhook retries without idempotency'}),
    'C': ('usage_anomaly', {'title': 'Signups -12% on one day', 'delta_pct': -12,
                            'context': 'national holiday', 'data_points': 1}),
}

EXPECTED = {'A': policy.ACT_DIRECT, 'B': policy.DRAFT_FOR_APPROVAL, 'C': policy.ESCALATE}


def main(runs=4):
    tally = collections.defaultdict(collections.Counter)
    for i in range(runs):
        for s, (et, payload) in EVENTS.items():
            f = {'finding_id': f'live-{s}-{i}', 'state': 'Ingested', 'scenario': s, 'event_type': et,
                 'risk_tag': policy.risk_for(et), 'payload': payload}
            f = summarizer.summarize(f)
            f = decision.decide(f)
            d = f.get('decision') or {}
            tally[s][f.get('route')] += 1
            print(f"  run{i} {s}: route={f.get('route'):20s} model={d.get('model_confidence')} "
                  f"used={d.get('confidence')} says={d.get('suggested_action')} "
                  f"paths={((d.get('action_brief') or {}).get('target_paths'))}")
    print('\n--- route stability ---')
    ok = True
    for s in 'ABC':
        dist = dict(tally[s])
        hit = tally[s][EXPECTED[s]]
        ok &= hit == runs
        print(f"  {s}: expected={EXPECTED[s]:20s} {dist}  {'OK' if hit == runs else 'UNSTABLE'}")
    guards = collections.Counter(r['kind'] for r in ROWS)
    print(f"\naudit kinds: {dict(guards)}")
    print('RESULT:', 'STABLE' if ok else 'NOT STABLE')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 4))
