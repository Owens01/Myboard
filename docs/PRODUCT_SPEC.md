# Product Spec (Prelint context)

The rules Prelint enforces on every pull request. Plain statements, no code.

## What this product is

An autonomous board member for founders who have no board. It watches company
signals, summarizes them into findings with evidence and confidence, decides
what to do, acts autonomously only when the risk tag allows it, verifies its
own work with real tests, passes everything through a governance checklist,
and logs every step. Sensitive work is drafted for human approval, never acted
on alone.

## Non-negotiable rules

1. Risk tags and routes are computed by deterministic code in
   `src/core/policy.py`. Model text must never set a risk tag or route.
2. Sensitive findings are never acted on autonomously, at any confidence.
3. A fix is verified only by a recorded pytest exit code from a real sandbox
   run (local `/tmp` or a Tenki session). A claimed green run is not evidence.
4. Summarizer evidence refs must be keys that literally exist in the finding
   payload. Payload keys `web`, `companies`, and `page` come from external
   research and are quotable evidence.
5. Tests are never modified by fixes. Diffs touch at most 4 files, at most
   200 lines, only inside the decision's target paths.
6. Every state transition writes an audit row before the state is persisted.
   The audit trail is append-only and is never updated or deleted.
7. External integrations (search, data, scraping, sandboxes, pipelines) are
   silent without their keys. No key must never break ingestion, tests, or
   the offline demo.

## Payload fields agents may add

- `web`: fresh search sources (title, url, snippet). From the research agent.
- `companies`: company enrichment rows (name, domain, detail). From the data agent.
- `page`: fetched page content as markdown. From the scraping agent.
