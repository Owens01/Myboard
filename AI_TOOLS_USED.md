# AI tools used

Recorded as required by the hackathon rules. Every tool listed below wrote or reviewed code,
docs or prompts for this repository.

| Tool | What it was used for |
|---|---|
| Cline (agentic coding assistant, GLM/Claude models) | Project engineering: AWS preflight, SAM template, Lambda code, tests, dashboard, docs, deploys |
| Querit | Research agent: fresh web sources attached to every ingested finding (src/core/web.py) |
| RocketRide | Portable stage pipelines (pipelines/*.pipe) + Cloud runs via scripts/rr_stage.py |
| Tenki | Verification sandbox: pytest runs inside a Tenki session (src/core/sandbox.py) |
| Prelint | Product review for every PR (prelint.json rules + docs/PRODUCT_SPEC.md context) |
| Glasser | Data agent: company enrichment (src/core/leads.py) |
| Apify | Scraping agent: page fetches as markdown (src/core/scrape.py) |
| (add more as they are used) | |
