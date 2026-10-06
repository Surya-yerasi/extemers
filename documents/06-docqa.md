# docqa: personal-document Q&A on Bedrock

docqa answers questions about a small set of personal documents (transcripts, certificates) kept
encrypted in S3. It is also a lab for comparing retrieval designs and evaluating them rigorously.
It is built in phases, and this document grows with each one.

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundations: registry, document bucket, login, web function, image pipeline, budget | Done |
| 2 | Ingestion: parse → chunk → embed → LanceDB on S3 | Deployed (Bedrock calls blocked until the account quota case is resolved) |
| 3 | Ask page with retrieval strategies and citations | Deployed (answers wait for Bedrock access) |
| 3b | Local mode: the same app on free Ollama models, documents kept on the laptop | **This PR** |
| 4 | Evaluation harness (retrieval metrics, RAGAS, latency, cost) | Planned |
| 5 | Dashboards and alarms | Planned |
| 6 | Agents (LangGraph), platform comparisons | Planned |

## Phase 1 architecture

![docqa Phase 1](diagrams/07-docqa-phase1.png)

<sub>Source: [diagrams/07-docqa-phase1.mmd](diagrams/07-docqa-phase1.mmd)</sub>

| Resource | Name | Notes |
|---|---|---|
| ECR repository | `docqa-dev` | Immutable tags (the git SHA), scan on push, keeps the last 15 images |
| S3 bucket | `docqa-dev-<account>` | SSE-KMS with bucket key, versioned (old versions expire after 30 days), TLS-only, all public access blocked, `prevent_destroy` |
| Lambda function | `docqa-dev-web` | Container image, arm64, 512 MB, 30 s, X-Ray active, JSON logs |
| Function URL | (generated) | Auth type `NONE`; **the app enforces login on every route except `/health`, `/login`, `/callback`, `/logout`** |
| Log group | `/aws/lambda/docqa-dev-web` | Encrypted with the docqa KMS key, 7-day retention |
| Cognito user pool | `docqa-dev` | Lite tier (free), no self sign-up, TOTP MFA required, 12+ character passwords, deletion protection |
| Cognito domain | `docqa-dev-<account>.auth.us-east-1.amazoncognito.com` | Classic hosted login |
| Cognito app client | `docqa-dev-web` | Public client: authorization code + PKCE, **no client secret** |
| SSM parameter | `/docqa/dev/config` | Plain `String`: Cognito domain, client ID, issuer, allowed redirect URIs, bucket name |
| Budget | `docqa-dev-account-monthly` | **Account-wide** $10/month; email at $5 actual and $10 forecast |

All of them carry `app=docqa`, through a provider alias with its own `default_tags`.

## Design decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D-01 | **One container image on Lambda, behind a Function URL** | Near-zero cost (Lambda free tier, no hourly resources), HTTPS built in, 15-minute timeout (room for agent loops), and a standard image that can move to AgentCore or ECS unchanged | ECS Fargate + ALB (about $35/month idle), AgentCore Runtime (needs a separate UI host), API Gateway (30 s cap, adds nothing here), CloudFront (not needed) |
| D-02 | **Lambda Web Adapter + FastAPI** | The same uvicorn server runs locally and in Lambda, with no Lambda-specific handler code | Mangum (ASGI shim), Powertools resolver (not a web framework) |
| D-03 | **Cognito hosted login, code + PKCE, public client** | Standard OIDC; the free tier covers it; PKCE means no client secret to store or rotate | Home-made password login; confidential client with a secret in SSM |
| D-04 | **The session cookie is Cognito's ID token, re-verified on every request** | No app signing secret at all; JWKS verification checks signature, issuer, audience, expiry and `token_use` | Server-side sessions (need storage); app-signed cookies (need a secret, and the read-only plan role cannot decrypt SecureString parameters) |
| D-05 | **Config in one SSM `String` parameter, read at cold start** | Breaks the Terraform cycle: Cognito needs the Function URL for its callback, and the function would otherwise need Cognito's IDs in its environment. Plain `String` because nothing in it is secret | Lambda environment variables (cycle); SecureString (the plan role cannot decrypt it) |
| D-06 | **Account-wide budget** | On-demand Bedrock usage is not tagged per app, so a tag-filtered budget would miss the most variable cost | Budget filtered on `app=docqa` |
| D-07 | **ECR created first in Deploy (`-target`)** | Lambda cannot be created before its image exists; a targeted apply of the registry alone is idempotent and runs before the image push | A second Terraform root just for ECR; manual first push |
| D-08 | **No reserved concurrency** | The account's Lambda concurrency quota is 10, and AWS requires at least 10 to stay unreserved, so nothing can be reserved. The quota itself caps concurrency at 10 | Raising the quota (free, via Service Quotas) would allow a per-function cap later |

## Phase 2: ingestion

![docqa ingestion](diagrams/08-docqa-ingestion.png)

<sub>Source: [diagrams/08-docqa-ingestion.mmd](diagrams/08-docqa-ingestion.mmd)</sub>

| Stage | Implementation | Notes |
|---|---|---|
| Trigger | S3 notification on `raw/` → `docqa-dev-ingest` (async) | Same image as the web app, different CMD (`docqa.ingest_app`). No Function URL. Failed ingests return 5xx; `AWS_LWA_ERROR_STATUS_CODES=500-599` turns that into a failed invocation, so S3 retries once |
| Identity | `doc_id` = SHA-256 of the object key (16 hex), `content_hash` = SHA-256 of the bytes | Re-uploading to the same key replaces the document; identical bytes reuse the cached parse |
| Text pages | `pypdf` **layout mode**, then `layout_to_markdown` | Default mode emitted one table cell per line (rows lost). Layout mode keeps rows; lines with ≥ 2 wide gaps become a Markdown table |
| Scanned pages and images | Page rendered at 144 dpi (`pypdfium2`) → **Nova 2 Lite** Converse with the image → Markdown | A page counts as scanned when it has fewer than 40 characters of text |
| Metadata | **Nova Micro** returns JSON: `doc_type`, `title`, `institution`, `person`, `date` | Tolerant parsing (code fences, prose, bad JSON → defaults) |
| Parse cache | `parsed/<doc_id>.json` | Chunking/embedding experiments never pay for vision or metadata again |
| Chunking | Headings, paragraphs and tables kept whole; packed to ~400 tokens with ~60 tokens of overlap; oversized tables split by row **with the header repeated**; oversized text split on lines and sentences | Pure function, 13 unit tests |
| Contextual header | `"Transcript · Northfield State University · 2019 · p1"` prepended to `embed_text` only | `text` (shown to users, used by BM25) stays clean |
| Embeddings | **Titan Text Embeddings V2**, 1024 dims, normalised | 256/512 dims are later experiments |
| Index | **LanceDB** table `chunks__struct400__titan1024` at `s3://docqa-dev-<account>/lancedb/` | Vectors and a BM25 full-text index in one table; one table per chunking × embedding variant |
| Deletes | `ObjectRemoved` → remove the document's rows and its parse cache | |

**Ingest IAM (least privilege):** read `raw/*`; read/write/delete `parsed/*` and `lancedb/*`; list only
those prefixes; KMS decrypt/generate only via S3; `bedrock:InvokeModel` on exactly the Nova 2 Lite and
Nova Micro inference profiles (plus their foundation models in any region, as cross-region profiles
require) and Titan V2.

### Phase 2 decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D-09 | **Parse once, cache the Markdown in S3** | Vision is the only expensive step; chunking and embedding experiments re-read the cache | Re-parsing per experiment |
| D-10 | **pypdf layout mode → Markdown tables** | Found with the synthetic transcript: default extraction loses table rows, which breaks "what grade did I get in X?" | Default extraction; Textract (costs money; only needed for scans, which already go to vision) |
| D-11 | **Vision only for pages without a text layer** | Digital PDFs are free to parse; vision tokens are spent only on scans and photos | Vision for every page (slower and pricier, risk of transcription errors) |
| D-12 | **One table per index variant** | Experiments (chunk size, embedding dims, contextual on/off) never overwrite each other; the eval harness compares tables | One table, rebuilt per experiment |
| D-13 | **No queue in front of LanceDB writes** | Uploads are rare and Lance commits use S3 conditional writes; a conflict fails the invocation and S3 retries | SQS FIFO for strict serialisation (more parts; S3 cannot target FIFO directly) |
| D-14 | **A synthetic corpus, generated by script** | Tests, CI and future evals never touch real personal documents, and every parsing path (text, table, scan, image) is covered | Testing on real documents |

### Blocker: Bedrock quotas (account verification)

All on-demand Bedrock quotas on this account are applied at **0**, below the AWS defaults (e.g.
8,000,000 tokens/min), and Service Quotas rejects increases. Calling Nova Micro in seven regions
(2026-10-06) showed the cause: every region either throttles or returns *"Your account is
currently being verified"*. Sydney answered once, then returned the same error. So this is
new-account verification, not a per-region quota, and switching regions is not a workaround.

The fix is on AWS's side: the error asks you to write to **aws-verification@amazon.com** if it
lasts more than 2 hours (in addition to the Support case). Until then, ingestion fails at the
first model call. Everything else is tested with fakes, and the whole app runs for real in
[local mode](#local-mode-ollama).

### Runbook: ingestion

| Command | What |
|---|---|
| `make docqa-upload-samples` | Upload the synthetic documents to `raw/samples/`; S3 triggers ingestion |
| `aws s3 cp my.pdf s3://docqa-dev-<account>/raw/` | Add your own document |
| `make docqa-ingest` / `make docqa-ingest KEYS="raw/a.pdf"` | Backfill or re-run from your laptop (prints per-document results) |
| `make docqa-stats` | Chunks in the index |
| `aws logs tail /aws/lambda/docqa-dev-ingest --follow` | Watch ingestion (logs carry IDs and counts, never document text) |

## Phase 3: asking questions

![docqa ask](diagrams/09-docqa-ask.png)

<sub>Source: [diagrams/09-docqa-ask.mmd](diagrams/09-docqa-ask.mmd)</sub>

The page at `/` has a question box, a **retrieval strategy** dropdown and a collapsible **debug
panel**. It calls `POST /api/ask` (login required) and renders the JSON result.

| Strategy | What it does | Model calls per question |
|---|---|---|
| `dense` | Embed the question (Titan V2), exact cosine search, top 5 | embed + generate |
| `bm25` | LanceDB full-text (BM25) search, top 5 | generate only |
| `hybrid` (default) | Dense top 20 + BM25 top 20, fused with **RRF** (k = 60), top 5 | embed + generate |
| `hybrid_rerank` | Hybrid top 20, re-ordered by **Cohere Rerank 3.5**, top 5 | embed + rerank + generate |

**Answer step:** the top 5 chunks become numbered sources `[1]`–`[5]`. Nova Micro answers from
them only, cites `[n]`, or replies `NOT_FOUND`. The app maps each `[n]` back to its chunk, file
and pages; markers outside 1–5 are not treated as citations. If retrieval finds nothing, the
model is not called at all.

**Debug panel**, per question:

| Field | Meaning |
|---|---|
| Timings (ms) | `embed`, `dense_search`, `bm25_search`, `fuse`, `rerank`, `generate`, `total` (measured in the function) |
| Tokens | Generation input/output (from Bedrock); embedding input (estimated as characters ÷ 4) |
| Cost | USD estimate from the price table in `domain/costs.py`; models without a price are listed rather than counted as $0 |
| Chunks | Each chunk's rank and score at every stage (`dense`, `bm25`, `rrf`, `rerank`); cited rows are highlighted |

**Web IAM (least privilege):** read `lancedb/*` only (no `raw/` or `parsed/`), list only that
prefix, KMS decrypt only via S3, and `bedrock:InvokeModel` on Nova Micro (profile + foundation
models), Titan V2 and Cohere Rerank 3.5. The function moves to 1 GB and 60 s.

**Errors:** Bedrock throttling returns 503 "model quota reached"; other AWS errors return 502;
invalid input (empty, over 1,000 characters, unknown strategy) returns 422.

### Phase 3 decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D-15 | **Own RRF in `domain/retrieval.py`** | About 20 lines, pure and unit-tested; keeps every stage's rank and score visible for the debug panel and the Phase 4 metrics | LanceDB's built-in hybrid query (hides the per-list ranks; a later comparison) |
| D-16 | **Exact (flat) vector search** | A few thousand chunks: exact search is fast and has perfect recall, so retrieval quality is not confounded by ANN settings | An IVF/HNSW index now (a Phase 4 experiment) |
| D-17 | **Separate read-only searcher** | `LanceChunkSearcher` never creates the table, so the web role needs no write access to the index | Reusing the writer class (would need `PutObject`) |
| D-18 | **Numbered sources + `NOT_FOUND` sentinel** | Citations are checked in code, not trusted; "not found" is a measurable outcome for refusal accuracy in Phase 4 | Free-form citations (file names), JSON output mode |
| D-19 | **Skip the model when retrieval is empty** | No grounding means no answer; saves a call | Letting the model say "not found" |
| D-20 | **`retrieve()` separate from `ask()`** | The eval harness can score retrieval (Recall@k, MRR, nDCG) without paying for generation | One combined call |
| D-21 | **No inline script or style** | The existing CSP (`default-src 'self'`) stays strict; model and document text is only inserted with `textContent` | Relaxing the CSP; a frontend framework |
| D-22 | **QA service built on first question** | `/health` and login never import LanceDB or call Bedrock, so they stay fast and work even if the index is unavailable | Building at startup |

### Runbook: asking

| Command | What |
|---|---|
| Open the Function URL and sign in | The ask page |
| `make docqa-ask Q="What was my GPA?" STRATEGY=hybrid_rerank` | The same pipeline from your laptop; prints the full JSON (answer, chunks, timings, cost) |
| `aws logs tail /aws/lambda/docqa-dev-web --follow` | `ask_completed` log lines: strategy, timings, tokens, cost (never the question or answer) |

Until the Bedrock quota case is resolved and the documents are ingested, expect: `bm25` →
"I couldn't find that in your documents." (no index yet, no model call); `dense`/`hybrid` →
"model quota reached".

## Local mode (Ollama)

The same app on free local models, while Bedrock is unavailable and later for $0 test runs. One
setting, `DOCQA_PROVIDER=local`, swaps the adapters in `pipelines/wiring.py`; the pipelines,
retrieval strategies, prompts, citations and debug panel are unchanged.

| Concern | Deployed (`bedrock`) | Local (`local`) |
|---|---|---|
| Documents, parse cache | S3 `raw/`, `parsed/` | `services/docqa/.data/raw/`, `.data/parsed/` |
| Index | `s3://…/lancedb/chunks__struct400__titan1024` | `.data/lancedb/chunks__struct400__bgem3_1024` |
| Embeddings | Titan Text Embeddings V2, 1024 dims | `bge-m3`, 1024 dims |
| Answers, metadata | Nova Micro | `qwen2.5:7b` |
| Scanned pages | Nova 2 Lite | `qwen2.5vl:7b` |
| Rerank | Cohere Rerank 3.5 | none: `hybrid_rerank` keeps the hybrid order and shows `local/no-rerank` |
| Login | Cognito | Cognito (the deployed pool; `localhost:8080/callback` is allow-listed) |
| Cost | Cents per month | $0 (debug panel shows `local/…` models at $0) |

`.data/` is git-ignored and docker-ignored, so personal documents placed there are never
committed or built into the image.

| Command | What |
|---|---|
| `brew install ollama && brew services start ollama` | Once: install and run Ollama |
| `make docqa-local-models` | Check Ollama and pull missing models (about 12 GB) |
| `make docqa-local-samples` | Copy the synthetic documents into `.data/raw/samples/` |
| `make docqa-local-ingest` | Ingest everything under `.data/raw/` (copy your own PDFs/images there too) |
| `make docqa-local-ask Q="What was the cumulative GPA?" STRATEGY=bm25` | Ask from the terminal (JSON) |
| `make docqa-local-dev` | The web page at http://localhost:8080 with local models |

### Local mode decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D-23 | **A provider switch at the composition root** | Proves the ports/adapters split: no pipeline code changes. The same switch later serves free eval runs | A separate local app; mocking Bedrock |
| D-24 | **Separate table name per embedding model** | Titan and bge-m3 vectors are not comparable; a local index can never be mixed into the deployed one (D-12) | One table, rebuilt per provider |
| D-25 | **Local files, not S3, in local mode** | Documents stay on the laptop; the parse cache in S3 is never filled with a different model's transcriptions | Local models reading and writing the S3 bucket |
| D-26 | **`bge-m3` and `qwen2.5` 7B** | bge-m3 gives 1024 dims like Titan and needs no query/document prefixes; qwen2.5 7B follows citation and JSON instructions well and fits in 18 GB of RAM next to the vision model | `nomic-embed-text` (needs task prefixes), `llama3.2:3b` (weaker at citations) |
| D-27 | **No local reranker** | A cross-encoder would add PyTorch to the project (gigabytes); the pass-through is labelled so results are never mistaken for reranked ones | `sentence-transformers` cross-encoder |

## Login flow

![docqa login](diagrams/06-docqa-login.png)

<sub>Source: [diagrams/06-docqa-login.mmd](diagrams/06-docqa-login.mmd)</sub>

Protections:
- `state` is checked with a constant-time comparison.
- The PKCE verifier never leaves the browser cookie and the token request.
- Redirect URIs are built from the `Host` header **only if they are on the allow-list**, which
  blocks host-header injection.
- Cookies are `HttpOnly` and `SameSite=Lax`, plus `Secure` over HTTPS.
- Responses carry CSP, `X-Frame-Options: DENY`, `nosniff` and `no-referrer` headers.
- Logs record the user's `sub` (an opaque ID), never the email address.

## Delivery

| Stage | What happens |
|---|---|
| PR | `docqa` job (ruff, mypy strict, pytest ≥ 90% coverage), `docqa-image` job (arm64 build via QEMU, no push), Terraform plan with `image_tag = <PR head SHA>` |
| Merge | Deploy: targeted apply of `module.docqa_ecr` → build and push `docqa-dev:<sha>` (skipped if it already exists) → plan → apply → smoke test (`/health` = 200, `/api/me` without login = 401) |

Images are built with `--provenance=false --sbom=false`, because Lambda rejects image indexes
that carry attestation manifests.

## Runbook

**Create your login** (once, after the first deploy):

```bash
POOL=$(terraform -chdir=infra/envs/dev output -raw docqa_user_pool_id)
aws cognito-idp admin-create-user --user-pool-id "$POOL" \
  --username you@example.com \
  --user-attributes Name=email,Value=you@example.com Name=email_verified,Value=true \
  --desired-delivery-mediums EMAIL
```

Cognito emails you a temporary password. At first sign-in you choose a new password and scan a
QR code with an authenticator app (TOTP).

**Budget alerts:** add a repository variable `ALERT_EMAILS` with one or more addresses,
comma-separated (e.g. `you@example.com`). Until it is set, the budget exists but sends no email.

**Run locally:**

| Command | What |
|---|---|
| `make docqa-check` | ruff, mypy, pytest |
| `make docqa-dev` | uvicorn with auto-reload on :8080, using the deployed `/docqa/dev/config` (log in through the real Cognito; `http://localhost:8080/callback` is allow-listed) |
| `make docqa-run` | The real arm64 image on :8080, with your current AWS credentials exported into the container for the run only |

**Plan or destroy manually:** `make tf-plan IMAGE_TAG=<sha in ECR>`. The bucket (your documents)
and the KMS key are protected by `prevent_destroy`; Cognito has deletion protection.
