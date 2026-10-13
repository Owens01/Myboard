"""Action agent (code model). Copies demo_company into a /tmp sandbox, runs real pytest,
asks the model for full-file replacements, writes + diffs + re-runs tests. Verification
truth comes only from the recorded subprocess result."""
import difflib
import hashlib
import json
import time
import os
import shutil
import subprocess
import sys

from core import router, storage

MAX_ATTEMPTS = 3


def _persist_attempt(finding, result, attempt_no):
    """Records the attempt on the finding and hands the patch to the next attempt."""
    files = dict(result.get('files_written') or {})
    explanation = files.pop('__explanation__', '')
    finding['last_attempt_files'] = files if not result.get('passed') else {}
    finding['attempts'] = (finding.get('attempts') or []) + [
        {'attempt': attempt_no, **{k: v for k, v in result.items() if k != 'files_written'}, 'explanation': explanation}
    ]
    return finding


def _run_pytest(cwd, timeout=60):
    env = dict(os.environ, PYTHONPATH=os.getcwd() if not cwd else cwd + os.pathsep + os.getcwd())
    started = time.time()
    try:
        from core import sandbox as tenki
        if tenki.available():
            return tenki.run_pytest(cwd, timeout=max(timeout, 120))
    except Exception:
        pass
    try:
        # No -x: report every failing test in one run so the retry loop gets full
        # information instead of peeling failures off one at a time.
        p = subprocess.run([sys.executable, '-m', 'pytest', '-q', 'tests'],
                           cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)
        out = (p.stdout + p.stderr)[-4000:]
        failed = sorted({l.split('::')[-1].split()[0] for l in out.splitlines() if l.startswith('FAILED')})
        return {'exit_code': p.returncode, 'passed': p.returncode == 0,
                'failed': failed, 'output_tail': out, 'duration_s': round(time.time() - started, 2)}
    except subprocess.TimeoutExpired:
        return {'exit_code': 124, 'passed': False, 'failed': ['<timeout>'], 'output_tail': 'pytest timed out', 'duration_s': timeout}


def _sandbox_copy(finding_id, source):
    dest = f'/tmp/run-{finding_id}'
    if os.path.exists(dest):
        shutil.rmtree(dest)
    shutil.copytree(source, dest)
    return dest


def _hashes(root):
    h = {}
    for dirpath, _dirs, files in os.walk(os.path.join(root, 'tests')):
        for fn in files:
            p = os.path.join(dirpath, fn)
            h[os.path.relpath(p, root)] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    return h


def act(finding, log=print):
    """Runs one attempt of the action loop. Returns updated finding (Verifying or Escalated)."""
    brief = (finding.get('decision') or {}).get('action_brief') or {}
    attempt_no = int(finding.get('attempt_count', 0)) + 1
    finding['attempt_count'] = attempt_no

    src = os.path.join(os.path.dirname(__file__), '..', 'demo_company')
    src = os.path.abspath(src)
    if not finding.get('scenario'):
        # Nothing to run tests against: either the board proposed no code change (advice),
        # or no sandbox is wired for this finding. We never invent a green run - we record
        # exactly what happened and let governance judge it.
        advice = not (brief.get('target_paths') or [])
        note = ('advice only: the board proposed no code change' if advice
                else 'no sandbox is configured for this finding')
        storage.audit(finding['finding_id'], 'attempt',
                      {'attempt': attempt_no, 'verified': False, 'reason': note,
                       'target_paths': brief.get('target_paths') or []})
        finding['verification'] = {'ran': False, 'reason': note, 'advice_only': advice}
        from core.state_machine import apply, VERIFIYING
        return apply(finding, VERIFIYING, note)
    if not os.path.isdir(src):
        storage.audit(finding['finding_id'], 'error',
                      {'stage': 'action', 'error': 'sandbox source missing', 'path': src})
        from core.state_machine import apply, ESCALATED
        return apply(finding, ESCALATED, 'sandbox source missing; nothing was verified')

    result = _run_sandbox(finding, brief, attempt_no, src, log)
    finding = _persist_attempt(finding, result, attempt_no)
    finding['verification'] = {'ran': True, 'exit_code': result['exit_code'],
                               'passed': result['passed'], 'failed': result['failed']}

    if result['passed']:
        from core.state_machine import apply, VERIFIYING
        return apply(finding, VERIFIYING, f'attempt {attempt_no} verified green')
    if attempt_no >= MAX_ATTEMPTS:
        storage.audit(finding['finding_id'], 'error', {'stage': 'action', 'attempts': attempt_no, 'result': result})
        from core.state_machine import apply, ESCALATED
        return apply(finding, ESCALATED, f'attempt {attempt_no} still red after {MAX_ATTEMPTS} attempts')
    # Red, but we still have attempts left: pass through Verifying so the attempt
    # result is recorded, then loop back to ActingDirect for the retry.
    from core.state_machine import apply, VERIFIYING, ACTING_DIRECT
    finding = apply(finding, VERIFIYING, f'attempt {attempt_no} red; verified')
    return apply(finding, ACTING_DIRECT, f'attempt {attempt_no} red; retrying')


def _dependency_sources(sandbox, target_paths, max_bytes=3000):
    """Source of the modules the target files import (excluding tests).

    When the task is 'migrate to the new dependency API', the agent has to be able
    to read that dependency. Deterministic: parsed from the target files' imports.
    """
    import re as _re
    refs = {}
    for rel in target_paths:
        p = os.path.join(sandbox, rel)
        if not os.path.exists(p):
            continue
        try:
            src = open(p).read()
        except OSError:
            continue
        for m in _re.finditer(r'^\s*(?:from|import)\s+([A-Za-z_][\w.]*)', src, _re.M):
            mod = m.group(1)
            base = mod.replace('.', '/')
            for cand in (base + '.py', base + '/__init__.py'):
                fp = os.path.join(sandbox, cand)
                if cand not in refs and not cand.startswith('tests/') and os.path.exists(fp):
                    refs[cand] = open(fp).read()[:max_bytes]
    return refs


def _run_sandbox(finding, brief, attempt_no, src, log):
    import scenarios as scen
    sandbox = _sandbox_copy(finding['finding_id'], src)
    scen.setup(sandbox, finding.get('scenario', ''))
    baseline = _run_pytest(sandbox)
    log(f"baseline exit={baseline['exit_code']}")
    tests_before = _hashes(sandbox)

    sys = ('You are the coding agent. Reply with JSON only: {"files":[{"path": str, "new_content": str}], '
           '"explanation": str}. Only files inside target_paths may change and tests are never modified. '
           'Read dependency_sources carefully: that is the source of the library you are migrating to. '
           'Call its API exactly as defined and RETURN the object it returns (e.g. its Result instance) - '
           'never substitute a bare True/False for the library return value. '
           'If previous_attempt_files is present, fix that attempt rather than starting over.')
    files = {}
    for rel in (brief.get('target_paths') or []):
        p = os.path.join(sandbox, rel)
        if os.path.exists(p):
            files[rel] = open(p).read()
    user = json.dumps({'goal': brief.get('goal', ''), 'target_paths': brief.get('target_paths', []),
                       'current_files': files,
                       'dependency_sources': _dependency_sources(sandbox, brief.get('target_paths') or []),
                       'previous_attempt_files': finding.get('last_attempt_files') or {},
                       'failing_output': baseline['output_tail']})
    resp = router.call_llm('code', router.LLMRequest(sys, user), finding_id=finding['finding_id'], log=log)
    applied, written = _apply_patch(sandbox, resp.text, brief.get('target_paths') or [], log, capture=True)
    result = _run_pytest(sandbox)
    tests_after = _hashes(sandbox)
    result['tests_unmodified'] = tests_before == tests_after
    result['applied_files'] = applied
    result['files_written'] = {k: v[:4000] for k, v in written.items()}
    result['explanation'] = (written.get('__explanation__') or '')[:600]
    diff = difflib.unified_diff([files.get(a, '') for a in applied], [open(os.path.join(sandbox, a)).read() for a in applied], lineterm='')
    diff_text = '\n'.join(diff)[:20000]
    result['diff_text'] = diff_text
    s3key = f'attempts/{finding["finding_id"]}/{attempt_no}.txt'
    storage.put_artifact(s3key, f'baseline_exit={baseline["exit_code"]}\nfiles={applied}\n\n' + diff_text)
    result['s3_key'] = s3key
    storage.audit(finding['finding_id'], 'attempt', {'attempt': attempt_no, 's3_key': s3key, 'result': result})
    return result


def _apply_patch(sandbox, text, target_paths, log, capture=False):
    """Writes only allowlisted, non-test files. Returns applied paths (and contents)."""
    written = {}
    try:
        obj = json.loads(__import__('re').search(r'\{.*\}', text or '', __import__('re').S).group(0))
    except (ValueError, AttributeError):
        return ([], written) if capture else []
    applied = []
    for f in obj.get('files', [])[:4]:
        rel = f.get('path', '')
        if not rel or any(x in rel for x in ('..',)) or rel.startswith('tests/'):
            continue
        if target_paths and not any(rel == t or rel.startswith(t.rstrip('/') + '/') for t in target_paths):
            continue
        p = os.path.join(sandbox, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, 'w').write(f.get('new_content', ''))
        applied.append(rel)
        written[rel] = f.get('new_content', '')
    written['__explanation__'] = str(obj.get('explanation', ''))
    return (applied, written) if capture else applied
