"""Boardagents state machine. Plain-dict transition table, audited transitions,
optimistic-concurrency persistence."""
from core import policy, storage

INGESTED = 'Ingested'
SUMMARIZED = 'Summarized'
DELIBERATING = 'Deliberating'
DECIDED = 'Decided'
ACTING_DIRECT = 'ActingDirect'
AWAITING_APPROVAL = 'AwaitingApproval'
VERIFIYING = 'Verifying'
GOVERNANCE_REVIEW = 'GovernanceReview'
ESCALATED = 'Escalated'
CLOSED = 'Closed'

TERMINAL = {CLOSED}
PARKED = {AWAITING_APPROVAL, ESCALATED}

TRANSITIONS = {
    INGESTED: {SUMMARIZED},
    # Two ways out of Summarized: a machine signal goes to the decision agent, a founder
    # topic goes to the board room. Only one of them is registered per finding (see
    # worker_handler._handlers), so A/B/C keep their exact original audit trail.
    SUMMARIZED: {DECIDED, DELIBERATING},
    DELIBERATING: {DECIDED, ESCALATED},
    DECIDED: {ACTING_DIRECT, AWAITING_APPROVAL, ESCALATED},
    ACTING_DIRECT: {VERIFIYING, ESCALATED},
    VERIFIYING: {GOVERNANCE_REVIEW, ACTING_DIRECT, ESCALATED},
    AWAITING_APPROVAL: {ACTING_DIRECT, CLOSED},
    GOVERNANCE_REVIEW: {CLOSED, ESCALATED},
    ESCALATED: {CLOSED},
}


class IllegalTransition(Exception):
    pass


def can(state, new_state):
    return new_state in TRANSITIONS.get(state, set())


def apply(finding, new_state, reason='', extra=None):
    """Audit the transition BEFORE persisting the state change (conditional on version)."""
    state = finding['state']
    if not can(state, new_state):
        raise IllegalTransition(f'{state} -> {new_state}')
    version = int(finding.get('version', 1))
    storage.audit(
        finding['finding_id'], 'transition',
        {'from': state, 'to': new_state, 'reason': reason, 'version': version},
    )
    finding['state'] = new_state
    finding['version'] = version + 1
    if extra:
        finding.update(extra)
    storage.put_finding(finding, expect_version=version)
    return finding


def apply_direct(finding, new_state, reason=''):  # used only by demo reset / admin flows
    finding['state'] = new_state
    return storage.put_finding(finding)


class Orchestrator:
    """Runs stage handlers until a terminal or parked state; idempotent per stage."""

    def __init__(self, handlers, log=print):
        self.handlers = handlers
        self.log = log

    def run(self, finding, max_stages=12):
        for _ in range(max_stages):
            state = finding['state']
            if state in TERMINAL or state in PARKED:
                return finding
            handler = self.handlers.get(state)
            if handler is None:
                raise IllegalTransition(f'no handler for state {state}')
            self.log(f"stage={state} finding={finding['finding_id']}")
            finding = handler(finding)
        return finding
