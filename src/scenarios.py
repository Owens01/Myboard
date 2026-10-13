"""Deterministic scenario setup steps (never the model) applied to the sandbox copy."""
import os
import shutil

A_TEST = '''\n"""Scenario A tests (written against the mailkit 2.x API). Never modified by the agent."""\nfrom app import notifier\nfrom app import reminders\nfrom vendor.mailkit import Result, OUTBOX\n\n\ndef test_notify_returns_result_ok():\n    r = notifier.notify("cust@example.com", "Welcome", "hello")\n    assert isinstance(r, Result) and r.ok is True\n\n\ndef test_notify_records_recipient():\n    notifier.notify("cust@example.com", "Welcome", "hello")\n    assert OUTBOX[-1]["recipient"] == "cust@example.com"\n\n\ndef test_reply_to_supported():\n    r = notifier.notify("a@x.com", "s", "b", reply_to="support@x.com")\n    assert r.ok is True\n    assert OUTBOX[-1]["reply_to"] == "support@x.com"\n\n\ndef test_reminder_returns_result():\n    r = reminders.send_reminder("u@x.com", "invoice due")\n    assert isinstance(r, Result) and r.ok is True\n'''

B_TEST = '''\n"""Scenario B tests: webhook idempotency. Never modified by the agent."""\nfrom billing import charges, webhooks\n\n\ndef test_duplicate_webhook_is_ignored():\n    charges.reset()\n    webhooks.reset()\n    a = webhooks.handle_payment({"id": "p1", "customer": "c1", "amount": 1000})\n    b = webhooks.handle_payment({"id": "p1", "customer": "c1", "amount": 1000})  # retry\n    assert a["processed"] is True\n    assert b["processed"] is False\n    assert charges.total_charged("c1") == 1000\n'''

C_TEST = '''\n"""Scenario C tests: usage dip handling. Never modified by the agent."""\n\n\ndef test_no_action_taken_on_ambiguous_dip():\n    # Scenario C escalates to a human; the sandbox must be untouched.\n    assert True\n'''

A_BRIEF = {
    'goal': "Migrate every call site of vendored mailkit from 1.x to 2.x. The 2.x API is "
            "send_message(*, recipient, subject, body, reply_to=None) which returns a Result object "
            "with a .ok boolean. Required end state: (1) replace send(to, subject, body) with "
            "send_message(recipient=..., subject=..., body=...); (2) return the Result unchanged from "
            "the library - never convert it to a bare bool; (3) notifier.notify(to, subject, body, "
            "reply_to=None) and reminders.send_reminder(to, text, reply_to=None) must accept reply_to "
            "and forward it to send_message so callers can set the reply address.",
    'target_paths': ['app/notifier.py', 'app/reminders.py'],
}
B_BRIEF = {
    'goal': 'Add idempotency to billing/webhooks.py handle_payment: retrying the same event id '
            'must not charge twice (return processed=False for duplicates).',
    'target_paths': ['billing/webhooks.py'],
}


def setup(sandbox, scenario):
    """Applies the scenario setup to the sandbox copy. Deterministic code, not the model."""
    if scenario == 'A':
        shutil.rmtree(os.path.join(sandbox, 'vendor', 'mailkit'))
        shutil.copytree(os.path.join(sandbox, 'vendor', 'mailkit_v2'), os.path.join(sandbox, 'vendor', 'mailkit'))
        shutil.rmtree(os.path.join(sandbox, 'vendor', 'mailkit_v2'))
        with open(os.path.join(sandbox, 'tests', 'test_scenario_a.py'), 'w') as f:
            f.write(A_TEST)
    elif scenario == 'B':
        with open(os.path.join(sandbox, 'tests', 'test_scenario_b.py'), 'w') as f:
            f.write(B_TEST)
    elif scenario == 'C':
        with open(os.path.join(sandbox, 'tests', 'test_scenario_c.py'), 'w') as f:
            f.write(C_TEST)
    return os.path.join(sandbox, 'tests')


BRIEFS = {'A': A_BRIEF, 'B': B_BRIEF, 'C': {'goal': 'None; escalate to the human.', 'target_paths': []}}

# Deterministic handoff: a founder's topic may map to a code playbook, in which case the
# board's motion can be executed and verified by the real sandbox loop instead of only
# advised on. Keyword rules, not model judgement - the model never picks the sandbox.
CODE_PLAYBOOKS = {
    'A': ('mailkit', 'dependency advisory', 'end-of-life', 'eol', 'cve', 'upgrade the library'),
    'B': ('idempot', 'double-charg', 'duplicate charge', 'webhook retr', 'charged twice',
          'charge twice', 'overcharg'),
}


def playbook_for(message):
    """The code sandbox this topic belongs to, or '' when the answer is advice only."""
    low = (message or '').lower()
    for scenario, needles in CODE_PLAYBOOKS.items():
        if any(needle in low for needle in needles):
            return scenario
    return ''


def brief_for(scenario):
    """The operator-authored remediation playbook for a scenario (may be empty).

    The Decision agent's confidence/suggestion still drives the route; this
    playbook only constrains *what the fix is allowed to touch*, so a model
    cannot invent files that do not exist or drift outside the blast radius.
    """
    b = BRIEFS.get(scenario or '') or {}
    return {'goal': b.get('goal', ''), 'target_paths': list(b.get('target_paths') or [])}

