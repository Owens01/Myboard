#!/usr/bin/env python3
"""Puts a real question to the deployed board and reports what happened.

Usage:
  python3 scripts/ask_board.py "our webhook retries are double-charging customers"
  python3 scripts/ask_board.py --reply "I disagree with the CFO, we cannot afford it"
  python3 scripts/ask_board.py --meeting m-abc123          # show a past session

Prints the participants with their real model ids, the vote tally, the resolution
and the policy route, then the minutes URL.
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = os.path.join(ROOT, '.dashboard_url')
STATE = os.path.join(ROOT, '.board_thread')
base = None

if 'SSL_CERT_FILE' not in os.environ and os.path.exists('/etc/ssl/cert.pem'):
    os.environ['SSL_CERT_FILE'] = '/etc/ssl/cert.pem'


def _get(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.loads(r.read().decode())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def _show(m):
    print(f"\n{'=' * 74}\nMEETING {m['meeting_id']}  round {m.get('round')}  status={m.get('status')}")
    print(f"topic: {m.get('topic')}")
    print(f"risk:  {m.get('risk_tag')} (category {m.get('topic_category')})")
    turns = m.get('turns') or []
    print(f"\n{'seat':<20} {'model':<30} {'vote':<12} conf")
    for t in turns:
        print(f"{(t.get('role') or '')[:19]:<20} {(t.get('model_id') or '')[:29]:<30} "
              f"{(t.get('vote') or ''):<12} {t.get('confidence')}")
    tally = m.get('tally') or {}
    if tally:
        print(f"\nvote: aye {tally.get('aye', 0)}  nay {tally.get('nay', 0)}  "
              f"conditional {tally.get('conditional', 0)}  abstain {tally.get('abstain', 0)}")
    res = m.get('resolution') or {}
    if res:
        print(f"\nmotion: {res.get('motion')}")
        print(f"{res.get('recommendation', '')}")
        for c in res.get('consensus') or []:
            print(f"  agreed: {c}")
        for c in res.get('dissent') or []:
            print(f"  DISSENT: {c}")
    f = m.get('finding') or {}
    dec = m.get('decision') or f.get('decision') or {}
    print(f"\nroute: {f.get('route')}  |  model confidence {dec.get('model_confidence')} "
          f"-> applied {dec.get('confidence')}")
    for a in (m.get('audit') or []):
        if a.get('kind') == 'policy_override':
            print(f"POLICY OVERRIDE: model said {a['payload'].get('model_suggested')}, "
                  f"policy imposed {a['payload'].get('policy_route')} ({a['payload'].get('risk_tag')})")
        if a.get('kind') == 'constraint':
            print(f"GUARD {a['payload'].get('guard')}: {a['payload'].get('reason')}")
    cost = m.get('cost') or {}
    print(f"\ncost: in {cost.get('tokens_in')} / out {cost.get('tokens_out')} "
          f"tokens, {cost.get('latency_ms')} ms of model time")
    print(f"minutes: {base}/api/meetings/{m['meeting_id']}/minutes.md")
    print(f"dashboard: {base}/")


def main():
    global base
    base = open(API).read().strip().rstrip('/') if os.path.exists(API) else None
    if not base:
        print('no .dashboard_url; deploy first or pass --url')
        return 1
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 1

    if args[0] == '--meeting':
        m = _get(f"{base}/api/meetings/{args[1]}")
        _show(m)
        return 0

    reply = args[0] == '--reply'
    approve = '--approve' in args
    args = [a for a in args if a != '--approve']
    message = ' '.join(args[1:] if reply else args)
    body = {'message': message}
    if reply and os.path.exists(STATE):
        body['thread_id'] = open(STATE).read().strip()

    out = _post(f"{base}/api/board", body)
    print(f"convened {out['meeting_id']} (round {out['round']}), "
          f"risk={out['risk_tag']} matched '{out['risk_basis']}' -> {out['topic_category']}")
    open(STATE, 'w').write(out['thread_id'])

    meeting, approved = None, False
    for _ in range(60):
        time.sleep(3)
        meeting = _get(f"{base}/api/meetings/{out['meeting_id']}")
        f = meeting.get('finding') or {}
        if f.get('state') == 'AwaitingApproval' and approve and not approved:
            print('  parked for approval -> approving as the founder')
            _post(f"{base}/api/findings/{f['finding_id']}/approve",
                  {'note': 'approved after reading the diff and the test evidence'})
            approved = True
            continue
        if meeting.get('status') not in ('convened', 'in_session') and f.get('state') in ('Closed', 'Escalated'):
            break
    if not meeting:
        print('meeting never appeared')
        return 1
    _show(meeting)
    return 0


if __name__ == '__main__':
    sys.exit(main())
