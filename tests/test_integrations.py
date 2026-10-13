import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core import leads, sandbox, scrape


def test_sandbox_unavailable_without_key_or_cli(monkeypatch):
    monkeypatch.delenv('TENKI_API_KEY', raising=False)
    assert sandbox.available() is False


def test_tenki_run_parses_exit_code(monkeypatch, tmp_path):
    (tmp_path / 'tests').mkdir()
    (tmp_path / 'tests' / 'test_x.py').write_text('def test_x(): pass')

    class P:
        returncode = 0
        stdout = '1 passed\n__EXIT__:0\n'
        stderr = ''

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if 'create' in cmd:
            p = P()
            p.stdout = 'abc123\n'
            return p
        return P()

    monkeypatch.setattr(sandbox.subprocess, 'run', fake_run)
    out = sandbox.run_pytest(str(tmp_path))
    assert out['passed'] is True and out['sandbox'] == 'tenki'
    assert any(c[2] == 'terminate' for c in calls)


def test_leads_and_scrape_silent_without_keys(monkeypatch):
    monkeypatch.delenv('GLASSER_API_KEY', raising=False)
    monkeypatch.delenv('APIFY_TOKEN', raising=False)
    assert leads.search_companies('acme') == []
    assert scrape.fetch_page('https://example.com') == ''


def test_scrape_returns_markdown(monkeypatch):
    class FakeResp:
        def __init__(self, payload):
            self.body = json.dumps(payload).encode()

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setenv('APIFY_TOKEN', 't')
    monkeypatch.setattr(scrape.urllib.request, 'urlopen',
                        lambda req, timeout=0: FakeResp(
                            [{'url': 'https://x', 'markdown': '# hi'}]))
    assert scrape.fetch_page('https://x') == '# hi'


def test_monitor_enrichment_skips_without_keys(monkeypatch):
    from agents import monitor
    from core import storage, web
    monkeypatch.setattr(storage, 'create_finding', lambda f: None)
    monkeypatch.setattr(storage, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(web, 'research', lambda q: [])
    monkeypatch.delenv('GLASSER_API_KEY', raising=False)
    monkeypatch.delenv('APIFY_TOKEN', raising=False)
    f = monitor.make_finding({'event_id': 'e1', 'event_type': 'dependency',
                              'payload': {'title': 'mailkit EOL'}})
    assert f['payload'] == {'title': 'mailkit EOL'}