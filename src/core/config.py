"""Boardagents configuration: env parsing, thresholds, model routing table."""
import json
import os


def env(name, default=''):
    return os.environ.get(name, default)


def env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).lower() in ('1', 'true', 'yes')


def cfg():
    """Snapshot of runtime config used across the pipeline."""
    region = env('BEDROCK_REGION') or env('AWS_REGION') or 'ap-south-1'
    return {
        'findings_table': env('FINDINGS_TABLE'),
        'audit_table': env('AUDIT_TABLE'),
        'feed_table': env('FEED_TABLE'),
        'meetings_table': env('MEETINGS_TABLE'),
        'bucket': env('ARTIFACTS_BUCKET'),
        'bedrock_region': region,
        # Bedrock API keys are mantle bearer tokens -> OpenAI-compatible surface.
        'bedrock_endpoint': env('BEDROCK_ENDPOINT') or f'https://bedrock-mantle.{region}.api.aws/v1',
        'bedrock_api_key': env('BEDROCK_API_KEY'),
        'use_fake_llm': env_bool('USE_FAKE_LLM', True),
        'max_llm_calls_per_finding': env_int('MAX_LLM_CALLS_PER_FINDING', 25),
        'llm_timeout_s': env_int('LLM_TIMEOUT_S', 60),
        'worker_name': env('WORKER_FUNCTION_NAME'),
        'admin_token': env('ADMIN_TOKEN'),
    }


# Confidence thresholds (Phase 0 default: high >= 0.85, medium 0.60-0.85, low < 0.60)
CONF_HIGH = 0.85
CONF_MEDIUM = 0.60

# Stages -> model tier. Real model IDs are frozen in Phase 0 after a live test;
# '' means the tier is not configured and the router falls back (or FakeLLM).
# Stages -> model tier. Model IDs were discovered live from GET /v1/models on the
# Bedrock mantle endpoint (Phase 0) and smoke-tested for strict-JSON compliance.
# Three different model families, so multi-model routing is real, not cosmetic.
MODEL_CONFIG = {
    'summarize':  {'tier': 'small',  'model_id': os.environ.get('MODEL_SMALL', 'zai.glm-4.7-flash'),       'max_tokens': 700,  'temperature': 0.2},
    'decide':     {'tier': 'strong', 'model_id': os.environ.get('MODEL_STRONG', 'deepseek.v3.2'),          'max_tokens': 1200, 'temperature': 0.2},
    'code':       {'tier': 'code',   'model_id': os.environ.get('MODEL_CODE', 'qwen.qwen3-coder-next'),    'max_tokens': 4000, 'temperature': 0.1},
    'governance': {'tier': 'small',  'model_id': os.environ.get('MODEL_SMALL', 'zai.glm-4.7-flash'),       'max_tokens': 600,  'temperature': 0.2},
    # Board room: every seat runs on its own model, so the audit trail shows real routing.
    'board_cto':    {'tier': 'code',    'model_id': os.environ.get('MODEL_CODE', 'qwen.qwen3-coder-next'),         'max_tokens': 1100, 'temperature': 0.3},
    'board_cfo':    {'tier': 'small',   'model_id': os.environ.get('MODEL_SMALL', 'zai.glm-4.7-flash'),            'max_tokens': 1100, 'temperature': 0.3},
    'board_risk':   {'tier': 'analyst', 'model_id': os.environ.get('MODEL_ANALYST', 'mistral.magistral-small-2509'), 'max_tokens': 1100, 'temperature': 0.3},
    'board_growth': {'tier': 'analyst2', 'model_id': os.environ.get('MODEL_ANALYST2', 'google.gemma-3-27b-it'),    'max_tokens': 1100, 'temperature': 0.3},
    'board_chair':  {'tier': 'strong',  'model_id': os.environ.get('MODEL_STRONG', 'deepseek.v3.2'),               'max_tokens': 1400, 'temperature': 0.2},
}


def model_for_stage(stage):
    c = dict(MODEL_CONFIG.get(stage, {'tier': 'small', 'max_tokens': 700, 'temperature': 0.2}))
    return c


def dumps(obj):
    return json.dumps(obj, separators=(',', ':'), sort_keys=True)
