#!/usr/bin/env python3
"""Live scenario runner: the REAL agents against REAL Bedrock, no AWS infra.

Runs scenarios A and B end to end in a sandbox on this machine using the same
code the Lambda runs. Storage is stubbed in memory. This is how we validate the
red -> green claim (and the policy override) before every deploy.

Run:  python3 scripts/live_scenario.py A|B|all [runs]
"""
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

from core import router, storage, state_machine as sm  # noqa: E402
from core import policy  # noqa: E402
import scenarios  # noqa: E402
from agents import summarizer, decision, action, governance  # noqa: E402

AUDIT = []
FINDINGS = {}


def _audit(fid, kind, payload, **extra):
    AUDIT.append({'finding_id': fid or '__system__', 'kind': kind, 'payload': payload, **extra})


storage.audit = _audit
storage.audit_for = lambda fid: [a for a in AUDIT if a['finding_id'] == fid]
storage.create_finding = lambda f: FINDINGS.setdefault(f['finding_id'], f)
storage.get_finding = lambda fid: FINDINGS.get(fid)
storage.put_finding = lambda f, expect_version=None: FINDINGS.__setitem__(f['finding_id'], f) or f
storage.put_artifact = lambda key, content: f'/tmp/{key}'
router.storage_audit = lambda *a, **k: None

EVENTS = {
    'A': ('dependency_advisory', {'title': 'mailkit 1.x EOL + CVE-2026-1337', 'package': 'vendor/mailkit',
                                  'severity': 'high', 'fix': 'upgrade to mailkit 2.x', 'cve': 'CVE-2026-1337'}),
    'B': ('billing_anomaly', {'title': 'Duplicate charges spike', 'customers_affected': 3,
                              'pattern': 'same customer+amount within 90s',
                              'suspected_cause': 'webhook retries without idempotency'}),
}


def run(scenario, log=print):
    et, payload = EVENTS[scenario]
    fid = f'live-{scenario}'
    f = {'finding_id': fid, 'state': sm.INGESTED, 'version': 1, 'scenario': scenario, 'event_type': et,
         'risk_tag': policy.risk_for(et), 'payload': payload, 'source': 'fixture', 'summary': None,
         'decision': None, 'route': None, 'attempt_count': 0, 'created_at': 0}
    FINDINGS[fid] = f
    f = summarizer.summarize(f, log=log)
    f = decision.decide(f, log=log)
    log(f"  route={f['route']} conf={f['decision']['confidence']} "
        f"brief_files={(f['decision']['action_brief'] or {}).get('target_paths')}")

    if f['state'] == sm.AWAITING_APPROVAL:
        f['approval'] = {'status': 'approved', 'note': 'reviewed by human', 'ts': 0}
        f = sm.apply(f, sm.ACTING_DIRECT, 'approved by human')

    guard = 0
    while f['state'] not in (sm.CLOSED, sm.ESCALATED, sm.AWAITING_APPROVAL) and guard < 10:
        guard += 1
        if f['state'] == sm.ACTING_DIRECT:
            f = action.act(f, log=log)
        elif f['state'] == sm.VERIFIYING:
            f = sm.apply(f, sm.GOVERNANCE_REVIEW, 'verified')
        elif f['state'] == sm.GOVERNANCE_REVIEW:
            f = governance.run(f, log=log)
        else:
            raise AssertionError(f'unhandled state {f["state"]}')

    attempts = f.get('attempts') or []
    log(f"  attempts={len(attempts)} results={[a.get('passed') for a in attempts]}")
    for a in attempts:
        log(f"    attempt {a['attempt']}: exit={a.get('exit_code')} applied={a.get('applied_files')}")
    log(f"  governance={(f.get('governance') or {}).get('verdict')} "
        f"failed_checks={[c['check'] for c in ((f.get('governance') or {}).get('checks') or []) if not c['pass']]}")
    kinds = [a['kind'] for a in AUDIT if a['finding_id'] == fid]
    log(f"  FINAL STATE={f['state']}  audit_rows={len(kinds)} kinds={sorted(set(kinds))}")
    return f


def main(arg='all', runs=1):
    which = 'AB' if arg == 'all' else arg.upper()
    results = {}
    for _ in range(runs):
        for s in which:
            print(f"=== scenario {s} ===")
            results.setdefault(s, []).append(run(s))
    print('\n--- summary ---')
    ok = True
    for s, fs in results.items():
        states = [f['state'] for f in fs]
        want = sm.CLOSED
        good = all(x == want for x in states)
        ok &= good
        print(f"  {s}: states={states} {'OK' if good else 'NOT CLOSED'}")
    print('RESULT:', 'PASS' if ok else 'FAIL')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else 'all',
                  int(sys.argv[2]) if len(sys.argv) > 2 else 1))
