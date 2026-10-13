# My AI Board Voted Against Me — So I Built It Again, on AWS

*How a solo founder shipped a governance-first agent system on AWS in one hackathon weekend — and what Bedrock, Lambda and DynamoDB taught me along the way.*

---

You're a solo founder. You have no CTO to say "that dependency is end-of-life," no CFO to say "why are we double-charging customers," and no board member to say — as mine did, on the record, 4 votes to 0 — *"we are not pausing all marketing to hire three salespeople in a pre-launch product. No."*

That last one isn't a story I made up. It's a real log from my hackathon project, **Boardagents**: an autonomous board of AI agents, running entirely on AWS, built in about two days, by one person, with no agent frameworks.

The pitch in one line: **founders without a board deserve a board that acts — and knows when not to.**

This post is about the second half of that sentence. The "when not to" is the entire project.

## What Boardagents actually does

Two things, both serverless on AWS:

1. **It watches and acts.** Signals flow in (dependency advisories, billing anomalies, usage dips). A pipeline summarizes each one with evidence, decides what to do, and — only when the risk level allows — patches the code itself in a sandbox. Verification is a *real* pytest run executed by a subprocess my code controls. The model saying "tests pass" is never used as truth; the recorded exit code is.

2. **It refuses.** A deterministic policy engine sits above the models. Billing anomaly? Sensitive — the most confident model in the world still gets drafted for human approval. Unknown event type? Fails closed to sensitive. And there's a governance checklist — tests unmodified, no secrets in the diff, no scope creep, approval record exists where required — where every check is code, and the model can only *add* concerns, never remove them.

The result I'm proudest of: when I told the board I wanted to pause all paid marketing and hire three salespeople immediately, four agents — a CTO seat on Qwen3-Coder, a CFO on GLM-4.7, Risk & Compliance on Magistral, Customer & Growth on Gemma-3-27b, all on Amazon Bedrock — debated, cited my actual runway numbers from a company context file, and voted **nay 4–0**. My own policy engine then capped the motion's confidence from the models' 0.90 down to 0.55 and escalated it to me, the human, with the reason attached: *"the board voted against acting: 4 against, 0 for."*

An agent system that argues with its owner and loses on purpose. That's the demo.

## The architecture, briefly

Everything is one CloudFormation stack deployed with AWS SAM in `ap-south-1`:

- **Two Lambda functions.** A public API on a Function URL (serves the dashboard and JSON API, kicks off work asynchronously — no judge should ever need an account) and a worker (1024 MB, 600s) that runs the pipeline: monitor → summarize → decide → act → verify → govern.
- **DynamoDB, three on-demand tables.** Findings with optimistic concurrency (conditional writes on a version attribute — a state machine that can't double-execute a stage), an append-only audit trail (every transition, every LLM call with model ID, tokens and latency), and a raw event feed.
- **S3** for patch diffs and test artifacts. **EventBridge** fires the monitor every 5 minutes. **CloudWatch** has every step.
- **Amazon Bedrock** does all inference, routed per stage: small/fast for summarize and governance judgment, a reasoning model for decisions, a coding model for patches. Four model families, swapped by config only, because the router has exactly one adapter.

There is no Step Functions, no LangChain, no framework. The state machine is a plain dict of legal transitions. The retry loop is 20 lines with exponential backoff and jitter. This was a deliberate constraint — the hackathon banned workflow engines — and it turned out to be the best part: when your state machine is your own code, "the board has no mandate" is one deterministic function, not a workflow hack.

## What fought back (the part worth publishing)

**1. Bedrock's control plane and data plane are gated separately — and nobody tells you.**
`list-foundation-models` cheerfully listed 50+ models. Every single `converse` call failed. First `AccessDeniedException: account is being verified` — no email, no ETA (it resolved itself in ~2 hours). Then `Operation not allowed`, with no pointer to *why*. The only way I could *infer* the cause: **Service Quotas showed `0.0` for on-demand inference on every model** — a proxy for "no model access granted." Anthropic models additionally require a First-Time-Use form the CLI can't even link to. If AWS reads this: one "why can't I invoke this model" diagnostic would have saved me an evening.

**2. The Bedrock API key is a different surface than the SDK's Converse path.**
With IAM inference blocked for me, Bedrock's newer **API keys** (the bedrock-mantle console) were the way in — but they're *bearer tokens on an OpenAI-compatible endpoint* (`bedrock-mantle.<region>.api.aws/v1`), not the SDK's `converse()`. The console doesn't explain the difference; I found the endpoint by probing with curl. One adapter later, all four models worked — the key ships as a SecureString reference in the SAM template, never in git. API keys are fantastic for hackathons; the docs just need one sentence: *"API key ⇒ this endpoint shape."*

**3. EventBridge Scheduler was blocked; EventBridge Rules worked.**
The newer Scheduler service simply failed to create on the free plan. The classic EventBridge *Rule* on a 5-minute schedule deployed instantly. Same outcome, but the error never suggested the fallback — I found it by bisecting a minimal template.

**4. New accounts can't use Lambda reserved concurrency** (AWS requires ≥100 unreserved). So all rate/cost protection moved into the application: a cap of 25 LLM calls per finding, a daily counter, and a global pause switch. Honest answer: app-level guards are more portable anyway.

**5. "Free plan" means "free with homework."** ~$100 in credits, a $20 Budget alarm created from the CLI (which, small thing, wants `BudgetLimit.Amount` as a *string*), and a measured cost per scenario run of a few cents — mostly Bedrock tokens, because everything else is on-demand and tiny.

## The lesson I'd put on a billboard

I went into this weekend thinking the hard part of agentic AI would be the models. It wasn't. The models are astonishing and cheap to wire up — four of them, routed by config, working within an hour once the access puzzle was solved.

The hard part is **everything you refuse to let the model decide**. The confidence ceiling that fires when a vote fails. The evidence check that invalidates hallucinated references. The hash comparison proving tests weren't touched. Every one of those guards is boring, deterministic code — and every one of them exists because I don't trust the most confident voice in the system, including when it's mine.

Solo founders don't need another chatbot. They need something that acts on the boring-but-critical stuff, shows its work, and stops itself when it shouldn't act. That's buildable *today*, on Lambda and DynamoDB and Bedrock, in a weekend, for the price of a coffee.

The board is now in session. It will still vote you down. Especially when you're wrong.

---

*Boardagents was built for the WeMakeDevs × AWS First Commit hackathon. Repo: github.com/SairajMN/BoardAgents — including `docs/runs/`, the full audit exports of real scenario runs, and `LEARNINGS.md`. Tools used: Cline as an AI coding assistant, under my direction, disclosed per the rules.*


