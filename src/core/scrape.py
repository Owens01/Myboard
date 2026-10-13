import json
import os
import urllib.request

API = 'https://api.apify.com/v2'
ACTOR = 'apify~website-content-crawler'


def fetch_page(url, max_pages=3):
    """Pulls one page as markdown via Apify. Returns text or ''."""
    token = os.environ.get('APIFY_TOKEN')
    if not token or not url:
        return ''
    body = json.dumps({
        'startUrls': [{'url': url}],
        'maxCrawlPages': max_pages,
        'crawlerType': 'cheerio',
    }).encode()
    req = urllib.request.Request(
        f'{API}/acts/{ACTOR}/run-sync-get-dataset-items',
        data=body, headers={'Content-Type': 'application/json',
                            'Authorization': f'Bearer {token}'})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            items = json.load(r) or []
    except Exception:
        return ''
    texts = [i.get('markdown') or i.get('text') or '' for i in items if isinstance(i, dict)]
    return '\n\n'.join(texts)[:8000]