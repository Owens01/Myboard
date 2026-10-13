# Learnings

Things I  did not know on Thursday and now do, and things that fought back.
One line each, logged as they happen.

## Day 1 (Sat 19 Sept)

- Bedrock's control plane (model catalog) and data plane (inference) are gated separately:
  `list-foundation-models` showed 50+ models, but every actual `converse` call failed.
- A brand-new-looking AWS account can return `Your account is currently being verified`
  from Bedrock runtime; it resolves itself in under ~2 hours.
- Bedrock service quotas show `On-demand model inference ... = 0.0` for every model when
  no model access has been granted — a quick way to read access state from the CLI.
- Anthropic models on Bedrock require a one-time First-Time-Use (use-case) form per account;
  `bedrock get-use-case-for-model-access` 404s until it is filled.
- A Bedrock API key (bedrock-mantle console) is region-scoped and is used as a bearer token;
  the SDK picks it up from `AWS_BEARER_TOKEN_BEDROCK`.
- The AWS Free plan gives access to over 90 services but requires care: Bedrock needs a valid
  payment method for Marketplace model subscriptions.
- Reserved Lambda concurrency is impossible on an account limited to 10 concurrent
  executions (AWS requires ≥100 unreserved), so all cost/rate protection must be app-level.
- Lambda's Python runtime ships only boto3 — running "real pytest" in the sandbox means
  vendoring pytest into the deployment package and setting `PYTHONPATH=/var/task` for the
  subprocess.
- `aws budgets create-budget` requires BudgetLimit.Amount as a **string**, not a number.
