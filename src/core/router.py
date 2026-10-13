"""Boardagents model router. One adapter (Bedrock Converse API), stage->tier config,
FakeLLM for offline tests, audit + call caps enforced here."""
import json
import re
import time
import urllib.request
import urllib.error

from core.config import cfg, model_for_stage


class LLMRequest:
    def __init__(self, system, user, max_tokens=None, temperature=None):
        self.system = system
        self.user = user
        self.max_tokens = max_tokens
        self.temperature = temperature


class LLMResponse:
    def __init__(self, text, model_id, usage, latency_ms, stop_reason):
        self.text = text
        self.model_id = model_id
        self.usage = usage or {}
        self.latency_ms = latency_ms
        self.stop_reason = stop_reason


class CallCapExceeded(Exception):
    pass


# ---------------------------------------------------------------- FakeLLM

def _fake_response(stage, request, finding=None, ctx=None):
    """Canned structured outputs per stage, so the pipeline is testable offline."""
    payload = (ctx or {}).get('payload') or {}
    ref = next(iter(payload), 'event')
    if stage == 'summarize':
        body = {
            'what_happened': f"[fake] Event captured: {payload.get('title', 'signal')} on {ref}.",
            'why_it_matters': '[fake] Impact assessed from the event payload and past history.',
            'confidence': 0.9,
            'evidence': [{'ref': ref, 'quote': str(payload.get(ref, ''))[:200]}],
        }
    elif stage == 'decide':
        risk = (ctx or {}).get('risk_tag', 'low')
        body = {
            'suggested_action': 'act_direct' if risk == 'low' else 'draft_for_approval',
            'rationale': '[fake] Decision derived from event severity and confidence.',
            'confidence': 0.9 if risk == 'low' else 0.75,
            'action_brief': {'goal': '[fake] resolve the finding', 'target_paths': []},
        }
    elif stage == 'code':
        body = {'files': [], 'explanation': '[fake] no patch generated in skeleton mode'}
    elif stage == 'governance':
        body = {'concerns': [], 'verdict': 'pass'}
    elif stage in ('board_cto', 'board_cfo', 'board_risk', 'board_growth'):
        keys = (ctx or {}).get('universe_keys') or ['message']
        ref = 'message' if 'message' in keys else keys[0]
        votes = {'board_cto': 'aye', 'board_cfo': 'conditional',
                 'board_risk': 'nay', 'board_growth': 'aye'}
        body = {
            'position': f'[fake] {stage} position on the founder\'s question',
            'reasoning': '[fake] reasoning grounded in the supplied context.',
            'evidence': [{'ref': ref, 'quote': '[fake] quoted from the context'}],
            'confidence': 0.8,
            'vote': votes.get(stage, 'aye'),
            'concerns': ['[fake] one stated concern'],
            'recommends_action': True,
        }
    elif stage == 'board_chair':
        body = {
            'motion': '[fake] proceed as the board proposed',
            'recommendation': '[fake] the board advises the founder to proceed with the guarded plan.',
            'consensus': ['[fake] the room agreed on the direction'],
            'dissent': ['[fake] the risk seat voted nay'],
            'confidence': 0.8,
            'suggested_action': 'draft_for_approval',
            'action_brief': {'goal': '[fake] resolve the founder question', 'target_paths': []},
            'minutes_summary': '[fake] minutes recorded for offline testing.',
        }
    else:
        body = {}
    return LLMResponse(json.dumps(body), 'fake-model', {'inputTokens': 0, 'outputTokens': 0}, 0, 'end_turn')


# ---------------------------------------------------------------- Bedrock (mantle, OpenAI-compatible)
# The Bedrock API key is a *mantle* bearer token, so inference goes through
# {bedrock_endpoint}/chat/completions (OpenAI-compatible) rather than the
# native /model/{id}/converse path. Stdlib only: no SDK, no signing.

def _chat_http(cfgv, req, model_id):
    url = cfgv['bedrock_endpoint'].rstrip('/') + '/chat/completions'
    messages = []
    if req.system:
        messages.append({'role': 'system', 'content': req.system})
    messages.append({'role': 'user', 'content': req.user})
    body = {'model': model_id, 'messages': messages,
            'max_tokens': req.max_tokens, 'temperature': req.temperature}
    http = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={
        'Content-Type': 'application/json',
        'Accept': 'application/json',
        'Authorization': f"Bearer {cfgv['bedrock_api_key']}",
    })
    started = time.time()
    try:
        with urllib.request.urlopen(http, timeout=cfgv['llm_timeout_s']) as resp:
            out = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:400]
        raise RuntimeError(f'bedrock {e.code}: {detail}')
    except Exception as e:  # timeouts, DNS, TLS
        raise RuntimeError(f'bedrock transport error: {type(e).__name__}: {e}')
    latency = int((time.time() - started) * 1000)
    if out.get('error'):
        raise RuntimeError(f"bedrock error: {json.dumps(out['error'])[:400]}")
    choice = (out.get('choices') or [{}])[0]
    msg = choice.get('message') or {}
    text = msg.get('content') or msg.get('reasoning_content') or ''
    if not text.strip():
        # Reasoning models can burn the whole budget on hidden thinking.
        raise RuntimeError(f"bedrock empty content (finish_reason={choice.get('finish_reason')})")
    usage = out.get('usage') or {}
    return LLMResponse(text, model_id, usage, latency, choice.get('finish_reason') or '')


def call_llm(stage, request, finding_id=None, ctx=None, log=print):
    """Routes one stage through the configured tier; audits the call and enforces caps."""
    cfgv = cfg()
    m = model_for_stage(stage)
    req = LLMRequest(
        system=request.system,
        user=request.user,
        max_tokens=request.max_tokens or m['max_tokens'],
        temperature=request.temperature if request.temperature is not None else m['temperature'],
    )
    if cfgv['use_fake_llm'] or not cfgv['bedrock_api_key'] or not m['model_id']:
        resp = _fake_response(stage, req, ctx=ctx)
    else:
        resp = _with_retries(lambda: _chat_http(cfgv, req, m['model_id']), log=log)
    storage_audit(finding_id, stage, resp)
    return resp


def _with_retries(fn, attempts=4, base=0.5, log=print):
    """Exponential backoff with jitter for throttling / transient transport errors."""
    import random
    last = None
    retryable = ('throttl', '429', '503', '502', '500', 'service unavailable',
                 'transport error', 'timed out', 'rate exceeded')
    for i in range(attempts):
        try:
            return fn()
        except RuntimeError as e:
            last = e
            if not any(t in str(e).lower() for t in retryable):
                raise
            wait = base * (2 ** i) + random.uniform(0, base)
            log(f'bedrock transient error, retry {i + 1}/{attempts} in {wait:.1f}s: {str(e)[:160]}')
            time.sleep(wait)
    raise last


def storage_audit(finding_id, stage, resp):
    from core import storage
    u = resp.usage or {}
    storage.audit(
        finding_id or '__llm__', 'llm_call', {'stage': stage, 'text_head': (resp.text or '')[:400]},
        stage=stage, model_id=resp.model_id,
        latency_ms=resp.latency_ms,
        tokens_in=u.get('prompt_tokens', u.get('inputTokens', 0)),
        tokens_out=u.get('completion_tokens', u.get('outputTokens', 0)),
    )


def extract_json(text, validators, repair_fn=None):
    """Parse strict JSON; validate with hand-written validators; one repair retry."""
    for attempt in (1, 2):
        m = re.search(r'\{.*\}', text or '', re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                for v in validators:
                    v(obj)
                return obj
            except (ValueError, AssertionError) as e:
                if attempt == 2:
                    raise ValueError(f'invalid LLM JSON after repair retry: {e}')
                text = repair_fn(text) if repair_fn else None
                if not text:
                    raise ValueError(f'invalid LLM JSON: {e}')
        elif attempt == 2:
            raise ValueError('no JSON object in LLM response')
    raise ValueError('no JSON object in LLM response')
