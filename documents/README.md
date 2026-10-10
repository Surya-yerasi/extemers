# extemers: platform documentation

`extemers` is a lab for building GenAI services on AWS the way a regulated enterprise would: infrastructure
as code, reviewed plans, keyless CI/CD, tagging for cost and cleanup. These documents describe the
**platform**, meaning everything that services share. Each service adds its own section as it is built.
The next service is **docqa**, a personal-document Q&A (RAG) app on Bedrock.

| # | Document | Read it when you want to know… |
|---|---|---|
| 1 | [High-level design](01-high-level-design.md) | What the platform provides and how its pieces connect |
| 2 | [Low-level design](02-low-level-design.md) | Every bootstrap resource, the deploy-role permissions, the env layout, scripts, Makefile |
| 3 | [Architecture decisions](03-architecture-decisions.md) | Why each platform choice was made, and what was rejected |
| 4 | [CI/CD](04-ci-cd.md) | What runs on a PR and on merge, and how GitHub authenticates to AWS |
| 5 | [Operations](05-operations.md) | First-time setup, adding a service, tagging, cost, teardown, incidents |
| 6 | [docqa](06-docqa.md) | The document Q&A service: architecture, decisions, login flow, runbook |
| 7 | [Measuring an AI application](07-ai-metrics.md) | What every metric on the Metrics tab means: latency, inference, retrieval, generation, online vs offline, statistics |

## Diagrams

Sources are Mermaid (`.mmd`) in [diagrams/](diagrams/), each with a rendered `.png`. Edit the `.mmd`
and run `make docs-diagrams` (Docker) to regenerate.

| Diagram | Shows |
|---|---|
| [01-system-context](diagrams/01-system-context.png) | Developer → GitHub → Actions → AWS; admin → bootstrap |
| [02-infrastructure](diagrams/02-infrastructure.png) | Bootstrap resources and the dev environment root |
| [03-ci-cd](diagrams/03-ci-cd.png) | CI and Deploy workflows, jobs and triggers |
| [04-oidc-auth](diagrams/04-oidc-auth.png) | How a GitHub job gets temporary AWS credentials |
| [05-tagging](diagrams/05-tagging.png) | How tags flow to the cleanup scripts and billing |
| [06-docqa-login](diagrams/06-docqa-login.png) | docqa sign-in: Cognito code + PKCE, token verification |
| [07-docqa-phase1](diagrams/07-docqa-phase1.png) | docqa Phase 1 resources |
| [08-docqa-ingestion](diagrams/08-docqa-ingestion.png) | docqa ingestion pipeline |
| [09-docqa-ask](diagrams/09-docqa-ask.png) | docqa question answering: strategies, fusion, rerank, cited answer |
| [10-docqa-evals](diagrams/10-docqa-evals.png) | docqa evaluation harness: corpus, golden set, metrics, CI gate |
| [11-docqa-agent](diagrams/11-docqa-agent.png) | docqa agent strategy: the LangGraph plan → retrieve → grade → generate → verify loop |

## History

The platform was proven end to end with a throw-away calculator Lambda behind API Gateway (PR #1).
It was deployed, smoke-tested and then removed through the pipeline itself. Its code and documents
remain in git history.
