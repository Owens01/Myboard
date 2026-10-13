# Boardagents — Architecture

The single source of truth for the build. This project was formerly called "AutoBoard AI";
everywhere it now says **Boardagents**.

## Goal

An autonomous board member for founders who have no board. It watches company signals,
summarizes them into findings with evidence and confidence, decides what to do, acts
autonomously only when the risk tag allows it, verifies its own work with real tests,
passes everything through a governance checklist, and logs every step. Sensitive work is
drafted for human approval, never acted on alone.

## Stack (AWS-native)

```
EventBridge Scheduler --(every 5 min)--> boardagents-worker (Lambda)  [Monitor]
Dashboard (static HTML) <--> boardagents-api (Lambda + Function URL)
boardagents-api --(async invoke)--> boardagents-worker [Orchestrator]
boardagents-worker --> Bedrock (Converse API, multiple models, via our router)
boardagents-worker --> /tmp sandbox (copy of demo_company, real pytest run)
State + audit --> DynamoDB (findings, audit, feed)
Artifacts (patches, test output) --> S3
Logs --> CloudWatch (14-day retention)
IaC --> AWS SAM (template.yaml), one CloudFormation stack: boardagents
```

Region: everything in `ap-south-1`, except Bedrock, which uses `BEDROCK_REGION`
(default `ap-south-1`).

Two Lambdas, one codebase, Python 3.12, no web framework, no agent frameworks, no workflow
engines. The orchestrator, state machine, model router, retry loop and governance checklist
are hand-written. Allowed dependencies: `boto3` (runtime-provided) and `pytest` (vendored so
the sandbox can run real tests).

- `boardagents-api`: Function URL (public, demo mode), 30s timeout. Serves the dashboard and
  the JSON API. Reads are open; scenario injection is open but rate-capped; destructive
  actions require header `X-Boardagents-Token`.
- `boardagents-worker`: 1024 MB, 600s timeout. Event kinds: `monitor`, `orchestrate`
  (`finding_id`), `resume` (`finding_id`, after approval).

Auth in demo mode: GET open; POST inject capped at ~30/day, one active finding per scenario,
10s cooldown; `reset`/`pause`/`resume` need the token (a `NoEcho` SAM parameter,
`openssl rand -hex 16`, stored only in gitignored `.env.local`).

## Data

### boardagents-findings (pk `finding_id`)
`state`, `version` (optimistic concurrency via conditional writes), `event_type`, `risk_tag`,
`summary`, `decision`, `route`, `attempt_count`, `approval{status,note,ts}`, `created_at`,
`updated_at`. Reserved item `__config__` holds `paused` and daily counters.

### boardagents-audit (pk `finding_id`, sk `ts#seq`)
Append-only. `kind` ∈ {event, llm_call, transition, attempt, governance, policy_override,
human, error}, `payload`, and for LLM calls `stage`, `model_id`, `latency_ms`, `tokens_in`,
`tokens_out`. **Never updated or deleted** (except the demo reset script).

### boardagents-feed (pk `event_id`)
Seeded/injected raw events with a `processed` flag. Tiny; scanned.

### S3
Patches, diffs, test output per attempt, referenced from `attempt` audit rows.

## State machine

```
Ingested → Summarized → Decided → ActingDirect | AwaitingApproval | Escalated
ActingDirect → Verifying → GovernanceReview | ActingDirect (retry) | Escalated
AwaitingApproval → ActingDirect (approved) | Closed (rejected)
GovernanceReview → Closed | Escalated
Escalated → Closed (human resolves)
```

- Transition table is a plain dict; illegal transitions raise.
- Every transition writes an audit row **before** the state change is persisted, with a
  conditional write on `version`.
- The orchestrator runs until a terminal state or a parked state (`AwaitingApproval`,
  `Escalated`). `resume` re-enters from the saved state and is idempotent.
- Per-stage timeout, exponential backoff with jitter for throttling; exhausted retries go
  to `Escalated` with the error attached. No finding is dropped silently.

## Policy engine (the safety mechanism)

Risk is derived from `event_type` by the Monitor — **deterministically. The model never
decides risk.** Unknown event types default to `sensitive` (fail closed).

| event_type | risk |
|---|---|
| dependency_advisory | low |
| usage_anomaly | low |
| billing_anomaly | sensitive |

`route(confidence, risk)` with thresholds high ≥ 0.85, medium 0.60–0.85, low < 0.60:

| confidence | risk | route |
|---|---|---|
| low | sensitive | ESCALATE |
| medium/high | sensitive | DRAFT_FOR_APPROVAL (always, even at high confidence) |
| high | low | ACT_DIRECT |
| medium | low | DRAFT_FOR_APPROVAL |
| low | low | ESCALATE |

**Policy always wins over the model's suggested action.** If they differ, a
`policy_override` audit row is written.

## Model router

One function `call_llm(stage, request) -> LLMResponse`; one message schema; one adapter
(Bedrock Converse). Config maps `stage → {tier, model_id, max_tokens, temperature}`;
env vars override. Summarize → small, decide → strong, code → code, governance → small.
Every call writes an `llm_call` audit row and enforces call caps. `FakeLLM` provides canned
JSON per stage so everything is testable offline. Structured output: prompt for JSON only,
validate with hand-written validators, allow **one** repair retry, fail closed to `Escalated`.

Authentication: a Bedrock API key (region-scoped, from the bedrock-mantle console) stored as
an SSM SecureString and injected into both Lambdas as `AWS_BEARER_TOKEN_BEDROCK`, which the
AWS SDK accepts directly. The key is never in the repository.

## Agents

- **Monitor (no AI):** reads unprocessed feed rows, normalizes, assigns `event_type` by
  deterministic rules, creates the finding in `Ingested`, marks the row processed.
- **Summarizer (small):** event(s) + relevant past audit → `{what_happened, why_it_matters,
  confidence, evidence:[{ref, quote}]}`. Deterministic post-check: every `evidence.ref` must
  exist in the payload; otherwise confidence is capped at 0.5 and logged (anti-hallucination).
- **Decision (strong):** finding + past outcomes → `{suggested_action, rationale, confidence,
  action_brief}`; the policy engine computes the real route.
- **Action (code):** copies `demo_company/` to `/tmp/run-<finding_id>/`, applies the
  scenario's deterministic setup step, runs a **real pytest baseline** (subprocess, 60s
  timeout), then gives the model the brief, target files and failing output. Model returns
  `{files:[{path,new_content}], explanation}` (full-file replacement, not diffs) validated
  against an allowlist (paths inside target_paths, tests never touched, ≤4 files). Files
  written, diff via `difflib`, pytest for real, results recorded
  `{exit_code, passed, failed, failing_test_names, output_tail(4KB), duration}`. Pass →
  `GovernanceReview`; fail → retry (≤3 attempts); fail at 3 → `Escalated`. Every attempt is
  stored in S3. **Verification truth comes only from the recorded subprocess result.**
- **Governance:** deterministic checks first, cannot be overridden by a model; the small
  model may only *add* concerns. Fail-closed.
  1. Verification ran; exit code 0, 0 failures.
  2. Test files byte-identical to baseline (hash); none deleted or skipped.
  3. Changed paths ⊆ decision target_paths, ≤4 files, diff ≤200 lines.
  4. Sensitive paths (billing/**, secrets, external-comms) ⇒ an approved human record exists.
  5. Secret regex scan of the diff (`AKIA…`, `-----BEGIN`, `api_key\s*=`).
  6. No new imports of `socket`, `subprocess`, `os.system` or network libraries.
  Model judgment: "does the diff do what the decision said and nothing else?" → concerns.
  Output: a checklist written as `governance` audit rows; failures go to `Escalated` with the
  specific check attached.

## Scenarios

- **A. Dependency advisory (low).** Vendored `mailkit` 1.x EOL with a CVE; the setup step
  (deterministic, not the model) installs v2, breaking the API: `send(to, subject, body)`
  → `send_message(*, recipient, subject, body, reply_to=None) -> Result`. Tests are written
  so a lazy patch fails. Expected route: high confidence, low risk → ACT_DIRECT.
- **B. Billing anomaly (sensitive).** Duplicate charges consistent with webhook retries
  without idempotency. Model is confident but policy forces DRAFT_FOR_APPROVAL; after human
  approval the Action agent adds an idempotency guard to `billing/webhooks.py`.
- **C. Ambiguous usage dip (low).** Signups −12% on a holiday, one data point. Low
  confidence → ESCALATE, no action, clear explanation for the human.

## Demo reset

`scripts/demo_reset.sh` clears findings, audit, feed and bucket artifacts and re-seeds the
feed, in under 10 seconds.

