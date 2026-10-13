#!/usr/bin/env python3
"""Builds src/repo_facts.json - a deterministic, secret-free manifest of the REAL project.

The board reasons over these facts instead of fixtures. Deterministic: sorted output,
stable content, safe to commit and diff. Run before `sam build`:

    python3 scripts/build_context.py

Also mirrors an operator profile into the Lambda package if one exists
(operator_profile.json is gitignored: it describes the founder's real situation).
"""
import json
import os
import re
import shutil
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'src', 'repo_facts.json')

SKIP_DIRS = {'.git', '.aws-sam', '__pycache__', '.venv', 'venv', 'node_modules',
             '.pytest_cache', '.idea', '.vscode', 'docs/runs'}
TEXT_EXT = {'.py', '.sh', '.yaml', '.yml', '.md', '.json', '.txt', '.ini', '.html', '.cfg'}
KEY_FILES = ['requirements.txt', 'pytest.ini', 'template.yaml', 'README.md', 'LICENSE']
SECRET_RE = re.compile(r'AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|'
                       r'(?:api[_-]?key|secret|password|token)\s*[:=]\s*["\'][^"\']{8,}', re.I)
MAX_FILE_BYTES = 4000
MAX_TREE = 250


def _walk():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        rel_dir = os.path.relpath(dirpath, ROOT)
        dirnames[:] = sorted(d for d in dirnames
                             if os.path.join(rel_dir, d).lstrip('./') not in SKIP_DIRS
                             and d not in SKIP_DIRS)
        for fn in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, fn), ROOT)
            if rel.startswith('..') or fn.startswith('.'):
                continue
            files.append(rel.replace(os.sep, '/'))
    return sorted(files)


def _git(*args):
    try:
        out = subprocess.run(['git', *args], cwd=ROOT, capture_output=True, text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ''
    except (OSError, subprocess.SubprocessError):
        return ''


def _aws_services():
    try:
        text = open(os.path.join(ROOT, 'template.yaml')).read()
    except OSError:
        return []
    types = set(re.findall(r'^\s*Type:\s*(AWS::[\w:]+)', text, re.M))
    return sorted(types)


def build():
    files = _walk()
    py = [f for f in files if f.endswith('.py')]
    loc = 0
    for f in py:
        try:
            loc += sum(1 for _ in open(os.path.join(ROOT, f), errors='ignore'))
        except OSError:
            pass

    key_files, redacted = {}, []
    for rel in KEY_FILES:
        # requirements/pytest.ini live under src/, docs at the root; try both.
        for candidate in (rel, os.path.join('src', rel)):
            p = os.path.join(ROOT, candidate)
            if not os.path.exists(p):
                continue
            try:
                text = open(p, errors='ignore').read()
            except OSError:
                continue
            if SECRET_RE.search(text):
                redacted.append(candidate)
                break
            key_files[candidate.replace(os.sep, '/')] = text[:MAX_FILE_BYTES]
            break

    deps = []
    for name, text in key_files.items():
        if name.endswith('requirements.txt'):
            deps = sorted(l.strip() for l in text.splitlines()
                          if l.strip() and not l.startswith('#'))
            break

    try:
        profile_text = open(os.path.join(ROOT, 'operator_profile.json')).read()
        profile = json.loads(profile_text)
    except (OSError, ValueError):
        profile = {}

    facts = {
        'project': os.path.basename(ROOT),
        'files_total': len(files),
        'python_files': len(py),
        'python_lines': loc,
        'tree': files[:MAX_TREE],
        'tests': [f for f in files if f.startswith('tests/') and f.endswith('.py')],
        'dependencies': deps,
        'key_files': key_files,
        'redacted_files': redacted,
        'git': {
            'head': _git('rev-parse', '--short', 'HEAD'),
            'commits': len([l for l in _git('log', '--oneline').splitlines() if l]),
            'first_commit': _git('log', '--reverse', '--format=%ad-%s', '--date=short').splitlines()[:1],
            'recent_commits': _git('log', '--format=%h %s', '-15').splitlines(),
        },
        'aws_resources': _aws_services(),
        'operator_profile': profile,
    }
    return facts


def main():
    facts = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w') as fh:
        json.dump(facts, fh, indent=2, sort_keys=True)
        fh.write('\n')

    profile = os.path.join(ROOT, 'operator_profile.json')
    bundled = 'not present (board runs with a neutral profile)'
    if os.path.exists(profile):
        shutil.copyfile(profile, os.path.join(ROOT, 'src', 'operator_profile.json'))
        bundled = 'bundled into src/operator_profile.json'

    print(f"wrote {os.path.relpath(OUT, ROOT)}")
    print(f"  files={facts['files_total']} python={facts['python_files']} lines={facts['python_lines']}")
    print(f"  deps={facts['dependencies']}")
    print(f"  aws resource types={len(facts['aws_resources'])}")
    print(f"  redacted (secret-like content)={facts['redacted_files']}")
    print(f"  operator profile: {bundled}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
