import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core import router


def test_fake_llm_summarize_shape():
    r = router._fake_response('summarize', None, ctx={'payload': {'title': 'x'}})
    obj = json.loads(r.text)
    assert obj['evidence'][0]['ref'] in ('title',)

def test_fake_llm_code_stage():
    r = router._fake_response('code', None, ctx={})
    assert json.loads(r.text)['files'] == []

def test_extract_json_validates():
    def must_have_goal(o):
        assert o.get('goal'), 'goal required'
    obj = router.extract_json('noise {"goal": "fix it"} trailing', [must_have_goal])
    assert obj['goal'] == 'fix it'

import pytest

def test_extract_json_rejects_bad():
    with pytest.raises(ValueError):
        router.extract_json('no json here', [])
    with pytest.raises(ValueError):
        router.extract_json('{"wrong": 1}', [lambda o: (_ for _ in ()).throw(AssertionError('no goal'))])

def test_model_config_tiers():
    from core.config import MODEL_CONFIG
    assert MODEL_CONFIG['summarize']['tier'] == 'small'
    assert MODEL_CONFIG['decide']['tier'] == 'strong'
    assert MODEL_CONFIG['code']['tier'] == 'code'
    assert MODEL_CONFIG['governance']['tier'] == 'small'
