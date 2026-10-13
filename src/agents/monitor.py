"""Monitor: no AI. Normalizes feed events, assigns event_type deterministically,
creates a finding in Ingested, marks the feed row processed."""
import uuid

from core import storage, web
from core.policy import risk_for

EVENT_TYPE_RULES = {
    'dependency': 'dependency_advisory',
    'advisory': 'dependency_advisory',
    'billing': 'billing_anomaly',
    'charge': 'billing_anomaly',
    'payment': 'billing_anomaly',
    'usage': 'usage_anomaly',
    'signup': 'usage_anomaly',
}


def classify_event_type(raw_type, source=''):
    text = f'{raw_type or ""} {source or ""}'.lower()
    for needle, et in EVENT_TYPE_RULES.items():
        if needle in text:
            return et
    # fail closed: unknown feeds are treated as sensitive
    return 'unknown_sensitive'


def make_finding(event):
    payload = dict(event.get('payload') or {})
    q = str(payload.get('message') or payload.get('title') or event.get('event_type') or '')[:200]
    if hits := web.research(q):
        payload['web'] = hits
    companies = (payload.get('company') or payload.get('vendor') or '').strip()
    if companies:
        from core import leads
        rows = leads.search_companies(f'{companies} {q}')
        if rows:
            payload['companies'] = rows
    for key in ('url', 'link', 'source_url'):
        if payload.get(key):
            from core import scrape
            text = scrape.fetch_page(payload[key])
            if text:
                payload['page'] = text
                break
    event = {**event, 'payload': payload}
    if event.get('mode') == 'board' or event.get('source') == 'founder':
        return _make_board_finding(event)
    event_type = classify_event_type(event.get('event_type') or event.get('type'), event.get('source', ''))
    fid = 'f-' + uuid.uuid4().hex[:10]
    ts = storage.now_ms()
    finding = {
        'finding_id': fid,
        'state': 'Ingested',
        'version': 1,
        'event_id': event.get('event_id'),
        'event_type': event_type,
        'risk_tag': risk_for(event_type),
        'scenario': event.get('scenario', ''),
        'payload': event.get('payload', {}),
        'source': event.get('source', ''),
        'summary': None,
        'decision': None,
        'route': None,
        'attempt_count': 0,
        'created_at': ts,
        'updated_at': ts,
    }
    storage.create_finding(finding)
    storage.audit(fid, 'event', {'source': event.get('source'), 'payload': event.get('payload', {})})
    return finding


def _playbook_for(message):
    """Deterministic topic -> code sandbox mapping (never decided by a model)."""
    try:
        import scenarios as scen
        return scen.playbook_for(message)
    except Exception:
        return ''


def _make_board_finding(event):
    """A founder topic. Risk comes from the topic keywords, deterministically, fail-closed."""
    from core import policy
    message = (event.get('payload') or {}).get('message', '')
    category, needle = policy.topic_basis(message)
    fid = 'f-' + uuid.uuid4().hex[:10]
    ts = storage.now_ms()
    finding = {
        'finding_id': fid,
        'state': 'Ingested',
        'version': 1,
        'event_id': event.get('event_id'),
        'mode': 'board',
        'event_type': 'founder_message',
        'topic_category': category,
        'risk_tag': policy.risk_for_topic(message),
        'meeting_id': event.get('meeting_id', ''),
        'thread_id': event.get('thread_id') or event.get('meeting_id', ''),
        # A deterministic handoff: only if the topic matches a known code playbook can the
        # board's motion be executed against a sandbox and verified by real tests.
        'scenario': _playbook_for(message),
        'payload': event.get('payload', {}),
        'source': 'founder',
        'summary': None,
        'decision': None,
        'route': None,
        'attempt_count': 0,
        'created_at': ts,
        'updated_at': ts,
    }
    storage.create_finding(finding)
    storage.audit(fid, 'event', {'source': 'founder', 'message': message,
                                 'topic_category': category, 'risk_basis': needle,
                                 'risk_tag': finding['risk_tag'],
                                 'meeting_id': finding['meeting_id']})
    return finding


def run_monitor(max_findings=3):
    """Processes up to max_findings unprocessed events; returns the finding ids made."""
    made = []
    for event in storage.unprocessed_events()[:max_findings]:
        finding = make_finding(event)
        storage.mark_processed(event['event_id'])
        made.append(finding['finding_id'])
    return made
