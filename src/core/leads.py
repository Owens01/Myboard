import json
import os
import shutil
import subprocess


def search_companies(q, count=5):
    """Company enrichment via Glasser. Returns provider rows or []."""
    key = os.environ.get('GLASSER_API_KEY')
    cli = shutil.which('glasser')
    if not key or not cli or not q:
        return []
    task = f'board-enrich-{os.getpid()}'
    try:
        s = subprocess.run([cli, 'search', q, '--use-case', 'enrich a board finding',
                            '--task', task, '-j'],
                           capture_output=True, text=True, timeout=30)
        found = json.loads(s.stdout or '{}')
        eps = found.get('endpoints') or found.get('results') or []
        if not eps:
            return []
        ep = eps[0]
        env = dict(os.environ, GLASSER_API_KEY=key)
        r = subprocess.run([cli, 'run', '-p', ep.get('provider', ''), '-e', ep.get('endpoint', ''),
                            '-i', json.dumps({'query': q, 'limit': count}),
                            '--task', task, '--wait', '-j',
                            '--idempotency-key', f'{task}-{abs(hash(q))}'],
                           capture_output=True, text=True, timeout=120, env=env)
        run = json.loads(r.stdout or '{}')
        out = run.get('output') or run.get('result') or run
        rows = out if isinstance(out, list) else out.get('results') or out.get('companies') or []
        return [{'name': x.get('name', ''), 'domain': x.get('domain', ''),
                 'detail': str(x)[:500]} for x in rows[:count]]
    except Exception:
        return []