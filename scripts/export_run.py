#!/usr/bin/env python3
"""Exports a full finding + audit trail to docs/runs/<scenario>-<finding>.json.

Judges reading only the repo can see the real evidence: every transition, LLM call
(stage + model id + tokens + latency), attempt (red/green), governance check and
policy override for a real run.

Usage:
  python3 scripts/export_run.py A
  python3 scripts/export_run.py --url https://... --token ... B
"""
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, '.dashboard_url')

# macOS system Pythons ship no CA bundle; /etc/ssl/cert.pem is present.
if 'SSL_CERT_FILE' not in os.environ and os.path.exists('/etc/ssl/cert.pem'):
    os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'


def _get(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header('X-Boardagents-Token', token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _post(url, body, token=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    if token:
        req.add_header('X-Boardagents-Token', token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _pick(base, scenario):
    data = _get(base + '/api/findings')
    mine = [f for f in data.get('findings', [])
            if f.get('finding_id', '').startswith('f-') and f.get('scenario') == scenario]
    mine.sort(key=lambda f: f.get('created_at', 0))
    return mine[-1] if mine else None


def _summarise(detail):
    audit = detail.get('audit') or []
    attempts = detail.get('attempts') or []
    gov = detail.get('governance') or {}
    d = detail.get('decision') or {}
    print(f"  state={detail.get('state')}  route={detail.get('route')}  attempts={detail.get('attempt_count')}")
    print(f"  decision: model_said={d.get('suggested_action')} model_conf={d.get('model_confidence')} "
          f"applied_conf={d.get('confidence')}")
    for a in attempts:
        print(f"  attempt {a.get('attempt')}: exit={a.get('exit_code')} passed={a.get('passed')} "
              f"files={a.get('applied_files')}")
    if gov:
        fails = [c['check'] for c in gov.get('checks', []) if not c['pass']]
        print(f"  governance: {gov.get('verdict')} ({len(gov.get('checks', []))} checks"
              f"{', failed=' + str(fails) if fails else ''})")
    print(f"  audit rows={len(audit)}  "
          f"overrides={len([a for a in audit if a.get('kind') == 'policy_override'])}  "
          f"constraints={len([a for a in audit if a.get('kind') == 'constraint'])}")
    for a in audit:
        if a.get('kind') == 'llm_call':
            print(f"    llm {a.get('stage'):10s} {a.get('model_id'):26s} {a.get('latency_ms')}ms "
                  f"in={a.get('tokens_in')} out={a.get('tokens_out')}")


def main():
    base = open(API).read().strip() if os.path.exists(API) else None
    args = sys.argv[1:]
    scenario = 'A'
    approve = '--approve' in args
    if '--url' in args:
        base = args[args.index('--url') + 1]
    for a in ('A', 'B', 'C', 'a', 'b', 'c'):
        if a in args:
            scenario = a.upper()
    if not base:
        print('no .dashboard_url and no --url given')
        return 1

    import time
    out_dir = os.path.join(ROOT, 'docs', 'runs')
    os.makedirs(out_dir, exist_ok=True)
    print(f'base={base}  scenario={scenario}  auto_approve={approve}')
    print('injected:', _post(base + '/api/inject', {'scenario': scenario}))

    fid, detail, approved = None, None, False
    for i in range(60):
        time.sleep(3)
        if fid is None:
            f = _pick(base, scenario)
            if not f:
                continue
            fid = f['finding_id']
            print(f'finding={fid} state={f["state"]}')
        detail = _get(f'{base}/api/findings/{fid}')
        state = detail.get('state')
        if state == 'AwaitingApproval' and approve and not approved:
            print('  parked for approval -> approving as the human operator')
            _post(f'{base}/api/findings/{fid}/approve', {'note': 'Reviewed the diff and the failing-test evidence.'})
            approved = True
            continue
        if state in ('Closed', 'Escalated'):
            break
    if not detail:
        print('no finding appeared')
        return 1

    _summarise(detail)
    path = os.path.join(out_dir, f'scenario-{scenario}-{fid}.json')
    with open(path, 'w') as fh:
        json.dump(detail, fh, indent=2, default=str)
    print('wrote', os.path.relpath(path, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
