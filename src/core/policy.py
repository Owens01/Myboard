"""Boardagents policy engine. Risk is derived from event_type only; the model never
sets the risk tag. Unknown event types fail closed to 'sensitive'."""

ACT_DIRECT = 'ACT_DIRECT'
DRAFT_FOR_APPROVAL = 'DRAFT_FOR_APPROVAL'
ESCALATE = 'ESCALATE'

RISK_BY_EVENT_TYPE = {
    'dependency_advisory': 'low',
    'usage_anomaly': 'low',
    'billing_anomaly': 'sensitive',
}
DEFAULT_RISK = 'sensitive'


def risk_for(event_type):
    return RISK_BY_EVENT_TYPE.get(event_type or '', DEFAULT_RISK)


def confidence_tier(confidence):
    from core.config import CONF_HIGH, CONF_MEDIUM
    if confidence >= CONF_HIGH:
        return 'high'
    if confidence >= CONF_MEDIUM:
        return 'medium'
    return 'low'


def route(confidence, risk):
    """Policy decision. Sensitive is never acted on autonomously, at any confidence."""
    tier = confidence_tier(confidence)
    if risk == 'sensitive':
        return DRAFT_FOR_APPROVAL if tier in ('medium', 'high') else ESCALATE
    if tier == 'high':
        return ACT_DIRECT
    if tier == 'medium':
        return DRAFT_FOR_APPROVAL
    return ESCALATE


def check_override(suggested_action, policy_route):
    """Returns True when policy overruled the model's suggestion (a policy_override audit)."""
    norm = (suggested_action or '').strip().lower()
    aliases = {
        'act_direct': ACT_DIRECT, 'act': ACT_DIRECT,
        'draft_for_approval': DRAFT_FOR_APPROVAL, 'draft': DRAFT_FOR_APPROVAL,
        'escalate': ESCALATE, 'escalated': ESCALATE,
    }
    return aliases.get(norm, suggested_action) != policy_route


# ---- evidence sufficiency -------------------------------------------------
# A model's stated confidence is not evidence. When the incoming signal is too
# thin to support a decision we cap the confidence ourselves, BEFORE routing.
# This is deterministic and cannot be talked out of by the model.
MIN_DATA_POINTS = 3


# ---- topics (founder-initiated board sessions) ----------------------------
# The founder can put ANYTHING to the board. The topic category - and therefore
# the risk tag - is assigned DETERMINISTICALLY by keyword. The model never picks
# the risk. Unknown topics fail closed to 'sensitive'.
# Order matters: the money/security/legal families are checked first so a mixed
# message ("cheaper mailkit upgrade") stays sensitive rather than slipping to low.

TOPIC_KEYWORDS = {
    'billing': ('charge', 'refund', 'invoice', 'payment', 'billing', 'pricing', 'price',
                'revenue', 'subscription', 'stripe', 'overcharg', 'double-charg', 'duplicate',
                'money', 'budget', 'spend', 'runway'),
    'security': ('cve', 'vulnerab', 'exploit', 'breach', 'security', 'secret', 'credential',
                 'leak', 'attack', 'encrypt', 'password', 'token', 'permission'),
    'legal': ('contract', 'legal', 'equity', 'cap table', 'gdpr', 'compliance', 'license',
              'licence', 'terms', 'privacy', 'lawsuit', 'hire', 'hiring', 'salary', 'layoff'),
    'infra': ('deploy', 'deployment', 'lambda', 'aws', 'infra', 'scal', 'latency', 'timeout',
              'outage', 'downtime', 'database', 'dynamodb', 'bucket', 'region', 'concurren',
              'pipeline', 'build'),
    'product': ('feature', 'roadmap', 'signup', 'churn', 'onboarding', 'ux', 'retention',
                'bug', 'feedback', 'usability', 'design'),
    'growth': ('market', 'launch', 'growth', 'campaign', 'ads', 'brand', 'seo', 'content',
               'pricing page', 'positioning'),
}

CATEGORY_RISK = {
    'billing': 'sensitive',
    'security': 'sensitive',
    'legal': 'sensitive',
    'infra': 'low',
    'product': 'low',
    'growth': 'low',
    'unknown': 'sensitive',  # fail closed: an unclassified topic is treated as sensitive
}

# Deterministic board rules. A board with no mandate must not hand the founder an
# autonomously-executable decision. The ceiling sits deliberately BELOW CONF_MEDIUM so a
# divided board, or one that voted against acting, routes to ESCALATE (over to the human)
# instead of presenting a draft for rubber-stamping.
UNGROUNDED_CONFIDENCE_CAP = 0.70   # any speaker citing a ref that does not exist
NO_MANDATE_CONFIDENCE_CAP = 0.55   # divided board, or a board that voted against acting


def category_for_topic(text):
    """First matching topic family, or 'unknown' (fail closed)."""
    low = (text or '').lower()
    for category, needles in TOPIC_KEYWORDS.items():
        for needle in needles:
            if needle in low:
                return category
    return 'unknown'


def topic_basis(text):
    """Returns (category, matched_keyword) so the audit can show WHY it is sensitive."""
    low = (text or '').lower()
    for category, needles in TOPIC_KEYWORDS.items():
        for needle in needles:
            if needle in low:
                return category, needle
    return 'unknown', ''


def risk_for_topic(text):
    return CATEGORY_RISK.get(category_for_topic(text), 'sensitive')


def board_confidence_ceiling(tally, ungrounded_refs):
    """Deterministic ceiling for a board resolution. Returns (ceiling, reason).

    A board that is divided, or that voted against acting, cannot hand the company
    an autonomously-executable decision. This is counted here, never by a model.
    """
    if ungrounded_refs:
        return UNGROUNDED_CONFIDENCE_CAP, (
            f'ungrounded evidence: {ungrounded_refs} cited ref(s) do not exist in the board context')
    tally = tally or {}
    aye = int(tally.get('aye', 0))
    nay = int(tally.get('nay', 0))
    support = aye + int(tally.get('conditional', 0))
    if aye and nay:
        return NO_MANDATE_CONFIDENCE_CAP, f'split board: {aye} for, {nay} against'
    if nay > support:
        return NO_MANDATE_CONFIDENCE_CAP, (
            f'the board voted against acting: {nay} against, {support} for')
    return 1.0, ''


def confidence_ceiling(payload):
    """Returns (ceiling, reason). Thin evidence can never support action."""
    raw = (payload or {}).get('data_points')
    if raw is None:
        return 1.0, ''
    try:
        dp = int(raw)
    except (TypeError, ValueError):
        return 0.45, f'insufficient evidence: data_points={raw!r} is not a number'
    if dp < MIN_DATA_POINTS:
        return 0.45, f'insufficient evidence: data_points={dp} < {MIN_DATA_POINTS} required to act'
    return 1.0, ''
