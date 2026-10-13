"""BoardContext: the deterministic evidence universe the board is allowed to cite.

Everything here is derived from real sources - the founder's own profile, a
reproducible manifest of the real project, the real audit history, the real
deployed cloud facts and the real policy table. The model never supplies context.

Any evidence.ref a member cites must exist as a key in this universe. Invented
refs are counted against the speaker and cap the resolution (see policy.board_confidence_ceiling).
"""
import json
import os

from core import policy
from core.config import cfg, MODEL_CONFIG

_here = os.path.dirname(os.path.abspath(__file__))
_FACTS = os.path.join(_here, '..', 'repo_facts.json')

MAX_VALUE = 900
TREE_VALUE = 1600


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def repo_facts():
    """The generated manifest of the real project (scripts/build_context.py)."""
    return _load_json(_FACTS)


def operator_profile():
    """The founder's real situation. Falls back to whatever is bundled or provided."""
    env_path = os.environ.get('OPERATOR_PROFILE')
    if env_path and os.path.exists(env_path):
        data = _load_json(env_path)
        if data:
            return data
    bundled = _load_json(os.path.join(_here, '..', 'operator_profile.json'))
    if bundled:
        return bundled
    return repo_facts().get('operator_profile') or {'company': 'unconfigured', 'team_size': 1}


def _clip(value, limit=MAX_VALUE):
    if isinstance(value, (dict, list)):
        text = json.dumps(value, sort_keys=True, default=str)
    else:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + '... [truncated]'


def history_facts(finding, limit=5):
    """Real prior findings on this topic, from the live store. Offline-safe."""
    try:
        from core import storage
        rows = storage.list_findings()
    except Exception:
        return []
    out = []
    for f in rows:
        if f.get('finding_id') in (finding.get('finding_id'), '__config__'):
            continue
        if f.get('topic_category') and f.get('topic_category') != finding.get('topic_category'):
            continue
        out.append({
            'finding_id': f.get('finding_id'),
            'state': f.get('state'),
            'route': f.get('route'),
            'event_type': f.get('event_type'),
            'outcome': (f.get('decision') or {}).get('suggested_action'),
            'confidence': (f.get('decision') or {}).get('confidence'),
            'attempts': f.get('attempt_count'),
        })
    out.sort(key=lambda r: r.get('finding_id') or '', reverse=True)
    return out[:limit]


def cloud_facts():
    """Real deployed facts, read from the runtime environment and config."""
    c = cfg()
    return {
        'region': c['bedrock_region'],
        'compute': 'AWS Lambda (boardagents-api + boardagents-worker)',
        'state': ['DynamoDB findings', 'DynamoDB audit (append-only)', 'DynamoDB feed', 'DynamoDB meetings'],
        'artifacts': 'S3 (attempt patches, test output, meeting minutes)',
        'schedule': 'EventBridge rule -> worker monitor',
        'model_gateway': c['bedrock_endpoint'],
        'models': {stage: m.get('model_id') for stage, m in MODEL_CONFIG.items()},
        'caps': {'max_llm_calls_per_finding': c['max_llm_calls_per_finding']},
    }


def evidence_universe(finding):
    """Builds {ref: short_value} - the only refs a board member may cite."""
    from core.config import CONF_HIGH, CONF_MEDIUM
    profile = operator_profile()
    facts = repo_facts()
    message = (finding.get('payload') or {}).get('message') or finding.get('message') or ''
    category = finding.get('topic_category') or policy.category_for_topic(message)

    refs = {
        'message': _clip(message, 2000),
        'founder.said': _clip(message, 2000),
        'profile.company': _clip(profile.get('company', '')),
        'profile.stage': _clip(profile.get('stage', '')),
        'profile.team_size': _clip(profile.get('team_size', '')),
        'profile.what_we_do': _clip(profile.get('what_we_do', '')),
        'profile.current_focus': _clip(profile.get('current_focus', '')),
        'profile.runway_months': _clip(profile.get('runway_months', '')),
        'profile.monthly_burn_usd': _clip(profile.get('monthly_burn_usd', '')),
        'profile.constraints': _clip(profile.get('hard_constraints', [])),
        'repo.project': _clip(facts.get('project', '')),
        'repo.counts': _clip({'files': facts.get('files_total'),
                              'python_files': facts.get('python_files'),
                              'python_lines': facts.get('python_lines')}),
        'repo.tree': _clip((facts.get('tree') or [])[:120], TREE_VALUE),
        'repo.dependencies': _clip(facts.get('dependencies', [])),
        'repo.tests': _clip(facts.get('tests', [])),
        'repo.git': _clip(facts.get('git', {})),
        'repo.key_files': _clip(sorted((facts.get('key_files') or {}).keys())),
        'infra.aws_resources': _clip(facts.get('aws_resources', [])),
        'cloud.facts': _clip(cloud_facts()),
        'history.prior_outcomes': _clip(history_facts(finding)),
        # Memory of the thread: what this same board already said in earlier rounds.
        'thread.prior_rounds': _clip((finding.get('payload') or {}).get('prior_rounds') or []),
        'policy.category': _clip(category),
        'policy.risk_tag': _clip(finding.get('risk_tag') or policy.risk_for_topic(message)),
        'policy.routes': _clip({'act_direct': 'policy allows autonomous action',
                                'draft_for_approval': 'human must approve before any action',
                                'escalate': 'the board cannot decide; parked for the human'}),
        'policy.thresholds': _clip({'confidence_high': CONF_HIGH, 'confidence_medium': CONF_MEDIUM}),
    }
    return {k: v for k, v in refs.items() if v not in ('', 'null', 'None', '[]', '{}')}


def ungrounded_refs(evidence, universe):
    """Refs cited that do not exist in the context - i.e. hallucinated citations."""
    return [e.get('ref') for e in (evidence or []) if (e or {}).get('ref') not in universe]


def render(universe, extra=None):
    """The context block handed to a board member. Deterministic ordering."""
    body = dict(universe)
    if extra:
        body.update(extra)
    return json.dumps(body, sort_keys=True, indent=1, default=str)
