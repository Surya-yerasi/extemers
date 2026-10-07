# docqa: personal-document Q&A on Bedrock

docqa answers questions about a small set of personal documents (transcripts, certificates) kept
encrypted in S3. It is also a lab for comparing retrieval designs and evaluating them rigorously.
It is built in phases, and this document grows with each one.

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundations: registry, document bucket, login, web function, image pipeline, budget | Done |
| 2 | Ingestion: parse → chunk → embed → LanceDB on S3 | Deployed (Bedrock calls blocked until the account quota case is resolved) |
| 3 | Ask page with retrieval strategies and citations | Deployed (answers wait for Bedrock access) |
| 3b | Local mode: the same app on free Ollama models, documents kept on the laptop | Done |
| 4 | Evaluation harness (retrieval metrics, RAGAS, latency, cost) | Done |
| 5a | Conversations, follow-up questions, per-question traces (Traces tab), EMF metrics | **This PR** |
| 5b | CloudWatch dashboard, alarms, email alerts | Next PR (needs a bootstrap update) |
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

## Phase 4: evaluation harness

![docqa evals](diagrams/10-docqa-evals.png)

<sub>Source: [diagrams/10-docqa-evals.mmd](diagrams/10-docqa-evals.mmd)</sub>

One command runs a golden question set through each retrieval strategy and writes a comparison
report. It works the same in local mode (free) and against Bedrock.

### The synthetic corpus and golden set

The corpus is one fictional person's archive of **15 documents**, generated by
`scripts/make_synthetic_docs.py` (byte-reproducible). It includes deliberate **look-alikes**,
so strategies can actually be told apart:
- two transcripts that both list *Machine Learning*, with grades A and B+
- two GPAs (3.86 and 3.72)
- two degree certificates
- two Brightwater letters
- two salaries (offer letter and current)

Two items are image-only (a scanned letter and a card photo), so the vision path is covered too.

`evals/golden/synthetic.jsonl` has **49 questions**:

| Category | Count | Tests |
|---|---|---|
| `fact` | 26 | One fact from one document |
| `table` | 8 | A row in a course table, often with a look-alike in the other transcript |
| `paraphrase` | 5 | Wording that does not appear in the document ("vacation" vs "paid time off") |
| `multi_doc` | 4 | Needs two documents (e.g. salary increase from offer letter to current) |
| `unanswerable` | 6 | Not in the documents; the right answer is "not found" |

Each answerable question lists its **evidence**: the document(s) and a short text that must be
in the retrieved chunk. Matching on text rather than chunk IDs keeps the set valid when chunking
changes. A test checks every evidence text against the generated PDFs, so the corpus and the
golden set cannot drift apart.

A **private golden set** over your real documents uses the same format. Keep it at
`services/docqa/.data/golden/private.jsonl` (git-ignored) and run with
`DATASET=.data/golden/private.jsonl`.

### Metrics

| Group | Metric | Definition |
|---|---|---|
| Retrieval (no model) | hit@k | A relevant chunk is in the top k |
| | recall@k | Share of the evidence items found in the top k (matters for multi-document questions) |
| | MRR | 1 / rank of the first relevant chunk |
| | nDCG@k | Rank-weighted gain; each evidence item counts once |
| Answers (no model) | correct | Answerable: every `must_contain` present, no `must_not_contain`, not refused. Unanswerable: refused |
| | refusal_accuracy / false_refusal_rate | Unanswerable questions refused / answerable questions wrongly refused |
| | citation_hit / citation_precision | At least one citation points to an evidence document / share of citations that do |
| LLM judge (optional) | RAGAS faithfulness, answer relevancy, context precision, context recall, answer correctness | Scored by a judge model through an OpenAI-compatible API (locally: Ollama) |
| Performance | latency P50/P95 | Retrieval stages and end to end, nearest-rank percentiles, after one untimed warm-up per strategy |
| | $ / question | From the price table (`domain/costs.py`); $0 for local models |

`must_contain` matches whole tokens, ignoring commas. Strings of one or two characters are
case-sensitive, so the grade "A" is not matched by the article "a", and "A-" is not "A".

### Baseline (local mode, 2026-10-06)

Local mode means bge-m3 embeddings, qwen2.5:7b answers and 49 questions; `hybrid_rerank` is
excluded locally (see D-31).

| Strategy | hit@1 | hit@5 | MRR | nDCG@5 | correct | refusal acc. | total P50 / P95 ms |
|---|---|---|---|---|---|---|---|
| dense | 0.860 | 0.977 | 0.909 | 0.923 | 0.918 | 1.000 | 2,404 / 3,698 |
| bm25 | 0.814 | 0.977 | 0.882 | 0.908 | 0.939 | 1.000 | 1,719 / 3,365 |
| **hybrid** | **0.860** | **1.000** | **0.924** | **0.945** | **0.959** | 1.000 | 1,962 / 3,684 |

**Findings:**
- **Hybrid is the only strategy that never misses.** Dense and BM25 each fail one paraphrase,
  for opposite reasons:
  - BM25 returns *nothing* for "How much vacation do I get at work?", because no word overlaps
    "paid time off".
  - Dense ranks the Lakeshore documents first for "Which company hired me after my master's?"
- **Look-alikes catch dense retrieval.** For bachelor's-degree grade questions, dense put the
  master's transcript first, and the model answered with the master's grade (table
  correctness 0.62 for dense, vs 0.75–0.88 for the others).
- **The evaluation found a product bug.** The model sometimes refused in prose ("the GPA is not
  provided in the document [1]") instead of `NOT_FOUND`, so the page showed a cited non-answer.
  Tightening the prompt took refusal accuracy from **0.83 to 1.00** for every strategy.
- **Model-side weakness:** every strategy retrieved the right transcript for "Which cloud
  course did I take in Fall 2020?", but qwen2.5:7b still answered "not found". This is a
  generation problem, not a retrieval one; it is worth re-checking with Nova or Claude.
- **Retrieval is cheap; generation dominates.** Retrieval takes 8–40 ms; answers take
  1.7–2.4 s at the median on the laptop.

**Reading the numbers:** with 43 answerable questions, one question is about 2.3 points. Two
local runs differed by one or two questions for the same strategy, so treat smaller differences
as noise.

### Phase 4 decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D-28 | **A purpose-built corpus with look-alikes** | With 4 documents every strategy scored 100% (the top 5 held everything); look-alikes make the differences measurable and reflect real archives (two transcripts, two offer letters) | Only the original 4 samples; real documents in CI |
| D-29 | **Evidence by document + text, not chunk ID** | One golden set scores every chunking variant | Chunk IDs (invalid after any chunking change) |
| D-30 | **Deterministic metrics by default; RAGAS judge opt-in** | Deterministic scores are free, fast (6 min locally for 147 answers) and repeatable. The local judge takes about 60 s per answer and grades with the same model family it is grading | Judge on every run |
| D-31 | **Strategy-major order, warm-up call, no `hybrid_rerank` locally** | Item-major order let Ollama reuse its prompt cache when two strategies sent the same prompt (rerank 790 ms vs hybrid 1,920 ms with identical prompts). Locally rerank is a pass-through, so its numbers would only repeat hybrid's | Item-major order; reporting identical strategies twice |
| D-32 | **BM25 regression gate in CI** | Real parsing + chunking + LanceDB full-text, no model calls: a change that hurts retrieval fails the PR | No CI gate (silent regressions); dense gate (CI has no embedding model) |
| D-33 | **RAGAS as a dev-only dependency** | It pulls in LangChain; the Lambda image stays lean. `langchain-community` is pinned to 0.4.1 because 0.4.2 removed a module RAGAS 0.4.3 imports | RAGAS in the runtime image |

### Runbook: evaluating

| Command | What |
|---|---|
| `make docqa-local-eval RETRIEVAL_ONLY=1` | Retrieval metrics only, in seconds |
| `make docqa-local-eval` | Full run, about 6 minutes locally: dense, bm25 and hybrid on 49 questions |
| `make docqa-local-eval STRATEGIES=hybrid LIMIT=10 JUDGE=local` | Add the RAGAS judge (about 1 minute per answer locally) |
| `make docqa-local-eval DATASET=.data/golden/private.jsonl` | Your private golden set |
| `make docqa-eval` | The same against Bedrock and the deployed index (after the quota is lifted; costs cents) |

Results go to `services/docqa/.data/evals/<time>-<provider>/`:
- `report.md`: the comparison tables
- `summary.json`: aggregates
- `records.jsonl`: every question × strategy with the retrieved chunks, answer, scores and timings

**Comparing an index variant**, e.g. smaller chunks: export the variant's settings, then ingest
into its own table and evaluate it.

```bash
export DOCQA_CHUNK_MAX_TOKENS=100 DOCQA_CHUNK_OVERLAP_TOKENS=15 \
       DOCQA_INDEX_TABLE=chunks__struct100__bgem3_1024
make docqa-local-ingest && make docqa-local-eval RETRIEVAL_ONLY=1
```

First experiment (2026-10-06): 100-token chunks made 22 chunks instead of 15 and **hurt**
retrieval. Hybrid hit@5 fell from 1.000 to 0.930 and BM25 from 0.977 to 0.907, because small
chunks separate facts from the heading that says which document (or which transcript) they
belong to. The 400-token default stays.

## Phase 5a: conversations and traces

The page now has two tabs:
- **Chat:** a sidebar of your conversations with *New conversation*, plus a message thread.
- **Traces:** every question's trace, opened by ID or from the link under each answer.

Each view has an address (`#/c/<conversation>`, `#/t/<trace>`), so the browser's back and
forward buttons and bookmarks work.

**IDs.** A conversation is `c_<16 hex>`; question *n* in it is turn `c_<16 hex>.n`. The turn
ID *is* the trace ID. It appears under the answer, in the `turn_completed` log line and on the
`trace_id` property of the CloudWatch metrics, so one ID ties together the page, the logs and
the metrics.

**Follow-up questions.** From the second question on, a `rewrite` step turns the follow-up into
a standalone question using the last 4 exchanges. Only the rewritten question is searched, and
the answer prompt still contains only that question and the retrieved sources, so earlier
answers cannot leak into grounding. The page shows *Searched as: "…"* when the wording changed.
Example (local mode): "And for my master's?" became "What was my cumulative GPA for my master's
degree?" and the answer was 3.72.

**Traces.** Each span records:
- its start offset and duration
- its status (an error span carries the exception)
- attributes: model, tokens, cost, top 5 chunks with scores per retrieval stage, and the rewrite
  input and output

The Traces tab shows them as a timeline; click a step to see its attributes. Also shown: the
chunks given to the model, the raw JSON, and in AWS the X-Ray trace ID and Lambda request ID.
**Failed questions are saved too**, with the failing span, so a throttled Bedrock call or a
stopped Ollama shows *where* it failed. The error response carries `X-Docqa-Trace-Id`.

**Storage.** Conversations are JSON in the same `BlobStore` as the documents:
- S3 when deployed, under `conversations/<hash of user sub>/`, KMS-encrypted like everything else
- `.data/conversations/` in local mode

Each user also has an `index.json`, so the sidebar needs one read. Users only ever see their
own conversations; any other ID returns 404, never 403, so IDs cannot be probed.

| Endpoint | What |
|---|---|
| `POST /api/ask` | `{question, strategy, conversation_id?}` → `{conversation, turn}` (a new conversation when no ID) |
| `GET /api/conversations` | Your conversations, newest first |
| `GET /api/conversations/{id}` | One conversation with all turns and traces |
| `DELETE /api/conversations/{id}` | Delete it |
| `GET /api/traces/{trace_id}` | One turn with its trace |

**Metrics (EMF).** In Lambda, each turn prints one Embedded Metric Format line, which
CloudWatch turns into metrics in namespace `docqa` (dimensions `service`, `environment`):
`AskLatency`, `RetrievalLatency`, `GenerationLatency`, `CostUSD`, `NotFound`, `ModelErrors`.
Six metrics stay inside the 10 free custom metrics. Strategy and trace ID are properties of the
line, not dimensions: they are queryable in Logs Insights and cost nothing extra. Locally,
metrics are switched off.

### Phase 5a decisions

| # | Decision | Why | Rejected |
|---|---|---|---|
| D-34 | **App-level traces stored with the turn** | Work identically locally and in Lambda; viewable in the app by ID; no extra service. The Web Adapter runs the app as a long-lived server, so the X-Ray SDK cannot attach per-request subsegments; the X-Ray trace ID is recorded for correlation instead | X-Ray SDK subsegments; OpenTelemetry collector (more moving parts than this needs) |
| D-35 | **Conversations in the existing bucket via `BlobStore`** | No new service, no deploy-role change, same encryption and access controls as the documents; local mode for free | DynamoDB (the production choice at scale: needs a bootstrap change; a later swap behind the same port) |
| D-36 | **Rewrite follow-ups; ground answers on the standalone question only** | Retrieval needs a self-contained query; keeping history out of the answer prompt keeps answers grounded in sources | Sending the full history to the answer model |
| D-37 | **Turn ID = trace ID = `<conversation>.<n>`** | A trace can be found from its ID alone, with no extra lookup table | Random trace IDs plus an index |
| D-38 | **Save failed turns** | The trace of a failure is the most useful one | Discarding failures (only a log line would remain) |
| D-39 | **EMF metrics with 2 dimensions** | No API calls, no IAM, free-tier; per-strategy detail via Logs Insights | Strategy as a dimension (4× the billed metrics) |
| D-40 | **Unconditional `s3:ListBucket` for both Lambda roles** | A prefix condition does not cover the existence check S3 makes on a missing object, so S3 returned 403 instead of 404. Confirmed with the IAM policy simulator; it would have failed every new-document ingest and every first conversation. Lists names only; contents stay prefix-scoped | Keeping the condition and listing instead of `HeadObject` (third-party code such as LanceDB makes its own existence checks) |

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
