import json
import os
import urllib.request

API = 'https://api.querit.ai/v1/search'


def research(q, count=5):
    key = os.environ.get('QUERIT_API_KEY')
    if not key or not q:
        return []
    req = urllib.request.Request(
        API, data=json.dumps({'query': q, 'count': count}).encode(),
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json',
                 'User-Agent': 'boardagents/1.0'})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            results = (json.load(r).get('results') or {}).get('result') or []
    except Exception:
        return []
    return [{'title': x.get('title', ''), 'url': x.get('url', ''), 'snippet': x.get('snippet', '')}
            for x in results[:count]]