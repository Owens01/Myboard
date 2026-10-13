import shutil
import subprocess
import tarfile

TENKI = shutil.which('tenki')


def available():
    return bool(TENKI and __import__('os').environ.get('TENKI_API_KEY'))


def run_pytest(local_dir, timeout=120):
    """Uploads the sandbox copy to a Tenki session, runs pytest there, returns
    the same result shape as the local runner."""
    import base64
    import io
    import os
    import time

    started = time.time()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode='w:gz') as t:
        t.add(local_dir, arcname='.')
    payload = base64.b64encode(buf.getvalue()).decode()

    script = (
        'set -e; rm -rf /tmp/ba && mkdir -p /tmp/ba && cd /tmp/ba && '
        f"echo '{payload}' | base64 -d | tar xzf - && "
        'python3 -m pytest -q tests 2>&1 | tail -c 4000; '
        'echo "__EXIT__:$?"'
    )
    try:
        name = f'ba-{os.path.basename(local_dir)}'
        p = subprocess.run([TENKI, 'sandbox', 'create', '--name', name,
                            '--cpu', '2', '--memory-mb', '2048'],
                           capture_output=True, text=True, timeout=180)
        sid = (p.stdout or '').strip().split()[-1]
        if p.returncode or not sid:
            raise RuntimeError(f'tenki create failed: {(p.stderr or "")[:200]}')
        r = subprocess.run([TENKI, 'sandbox', 'exec', '--session', sid,
                            '-c', script],
                           capture_output=True, text=True, timeout=timeout)
        subprocess.run([TENKI, 'sandbox', 'terminate', '--session', sid],
                       capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError, RuntimeError) as e:
        return {'exit_code': 124, 'passed': False, 'failed': ['<tenki-error>'],
                'output_tail': f'tenki sandbox failed: {e}'[:4000],
                'duration_s': round(time.time() - started, 2)}

    out = (r.stdout or '') + (r.stderr or '')
    code = 1
    if '__EXIT__:' in out:
        out, tail = out.rsplit('__EXIT__:', 1)
        try:
            code = int(tail.strip().split()[0])
        except (ValueError, IndexError):
            pass
    tail = out[-4000:]
    failed = sorted({l.split('::')[-1].split()[0]
                     for l in tail.splitlines() if l.startswith('FAILED')})
    return {'exit_code': code, 'passed': code == 0, 'failed': failed,
            'output_tail': tail, 'duration_s': round(time.time() - started, 2),
            'sandbox': 'tenki'}