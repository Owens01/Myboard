import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from agents import monitor
from core import storage, web


class FakeResp:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_no_key_no_search(monkeypatch):
    monkeypatch.delenv('QUERIT_API_KEY', raising=False)
    assert web.research('mailkit end of life') == []


def test_results_are_flattened(monkeypatch):
    monkeypatch.setenv('QUERIT_API_KEY', 'test-key')
    monkeypatch.setattr(web.urllib.request, 'urlopen',
                        lambda req, timeout=0: FakeResp({'results': {'result': [
                            {'title': 'Mailkit EOL', 'url': 'https://x', 'snippet': 'sunset'}]}}))
    assert web.research('mailkit eol') == [
        {'title': 'Mailkit EOL', 'url': 'https://x', 'snippet': 'sunset'}]


def test_monitor_attaches_web_evidence(monkeypatch):
    monkeypatch.setattr(storage, 'create_finding', lambda f: None)
    monkeypatch.setattr(storage, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(web, 'research', lambda q: [{'title': 'x', 'url': 'u', 'snippet': 's'}])
    f = monitor.make_finding({'event_id': 'e1', 'event_type': 'dependency',
                              'payload': {'title': 'mailkit EOL'}})
    assert f['payload']['web'][0]['title'] == 'x'


def test_monitor_without_hits_keeps_payload_untouched(monkeypatch):
    monkeypatch.setattr(storage, 'create_finding', lambda f: None)
    monkeypatch.setattr(storage, 'audit', lambda *a, **k: None)
    monkeypatch.setattr(web, 'research', lambda q: [])
    f = monitor.make_finding({'event_id': 'e2', 'event_type': 'dependency',
                              'payload': {'title': 'mailkit EOL'}})
    assert f['payload'] == {'title': 'mailkit EOL'}
