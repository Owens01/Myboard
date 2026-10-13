# Preflight — Boardagents (19 Sept 2026)

Account verified via `sts get-caller-identity` (root, profile `sai`, region `ap-south-1`).
Zero pre-existing resources: S3, DynamoDB, Lambda, CloudFormation, Scheduler, Logs all empty.

## Bedrock

| Probe | Result |
|---|---|
| `converse` in-region | `ValidationException: Operation not allowed` |
| `converse` via APAC profiles | `AccessDeniedException: account being verified` → later `Operation not allowed` |
| `list-foundation-models` | OK (control plane healthy, 50+ models) |
| Bedrock service quotas (all models, on-demand) | `0.0` — no inference granted |
| Anthropic First-Time-Use form | not submitted |

**Resolution:** Bedrock API key (bedrock-mantle console, ap-south-1) used as a bearer token.

The key unlocks a **different data plane** than the one the docs describe: it is a
*mantle* token that speaks the **OpenAI-compatible** surface, not `converse`:

| Probe | Result |
|---|---|
| `GET https://bedrock-mantle.ap-south-1.api.aws/v1/models` with bearer key | **200, 38 models** |
| `POST .../v1/chat/completions` with bearer key | **200, token usage returned** |
| `POST bedrock-runtime.../model/{id}/converse` with bearer key | `403 Operation not allowed` |

So the model router targets `/v1/chat/completions` with stdlib `urllib.request` and
`Authorization: Bearer <key>` — no SigV4, no SDK. The key is passed to the Lambdas as a
`NoEcho` CloudFormation parameter, read from the gitignored `.env` at deploy time.
See `docs/architecture.md` and `src/core/router.py`.

## Model tiers

Discovered live from `GET /v1/models` (38 ids) and then smoke-tested for **strict-JSON
compliance, latency and usage reporting** with `GET/POST .../v1/chat/completions`.
All in `ap-south-1` (`BEDROCK_REGION=ap-south-1`). Three different model families, so
multi-model routing is real rather than cosmetic.

| Tier | Stage | Model | Family | Measured |
|---|---|---|---|---|
| small | summarize, governance | `zai.glm-4.7-flash` | Z.ai / GLM | ~0.9–1.4 s, JSON OK |
| strong | decide | `deepseek.v3.2` | DeepSeek | ~1.9–2.4 s, JSON OK |
| code | action | `qwen.qwen3-coder-next` | Qwen | ~1.6 s, JSON OK |

Rejected during the smoke test: `minimax.minimax-m2.5` and `moonshotai.kimi-k2-thinking`
(reasoning-only models: `content: null`, all 500 tokens spent in `reasoning_content`).
`mistral.ministral-3-3b-instruct` and `google.gemma-3-4b-it` answered OK but were not
needed. Reproduce with `python3 scripts/live_smoke.py`.

## Constraints discovered

- **Reserved concurrency is impossible here.** Lambda account limit is 10 concurrent
  executions; AWS requires ≥100 unreserved to set any reserved concurrency. All safety
  is app-level: `MAX_LLM_CALLS_PER_FINDING=25`, daily counter, pause flag.
- **pytest must be vendored** into the worker package (Lambda Python runtime ships only
  boto3). Subprocess runs need `PYTHONPATH=/var/task`.
- Local Python is 3.14; Lambda runtime is 3.12 — code stays stdlib-only, no 3.13+ syntax.
- Account is on the AWS Free plan (~$100 credits, expire 2027-03-10). $20 budget alert
  `boardagents-alert` created.

## Cost per run

Measured after the first deployed runs; will be recorded here and in `LEARNINGS.md`.
