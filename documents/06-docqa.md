# docqa: personal-document Q&A on Bedrock

docqa answers questions about a small set of personal documents (transcripts, certificates) kept
encrypted in S3. It is also a lab for comparing retrieval designs and evaluating them rigorously.
It is built in phases, and this document grows with each one.

| Phase | Scope | Status |
|---|---|---|
| 1 | Foundations: registry, document bucket, login, web function, image pipeline, budget | Done |
| 2 | Ingestion: parse → chunk → embed → LanceDB on S3 | **This PR** (Bedrock calls blocked until the account quota case is resolved) |
| 3 | Ask page with retrieval strategies and citations | Planned |
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

### Blocker: Bedrock quotas

All on-demand Bedrock quotas on this account are applied at **0**, below the AWS defaults (e.g.
8,000,000 tokens/min). Service Quotas rejects increases because the default is already higher, so
this is an account-level restriction for new accounts and needs an **AWS Support case**. Until it
is lifted, ingestion fails at the first model call (metadata, vision or embedding). Everything else
(S3 trigger, parsing, chunking, LanceDB) is tested with fakes and inside the real container.

### Runbook: ingestion

| Command | What |
|---|---|
| `make docqa-upload-samples` | Upload the synthetic documents to `raw/samples/`; S3 triggers ingestion |
| `aws s3 cp my.pdf s3://docqa-dev-<account>/raw/` | Add your own document |
| `make docqa-ingest` / `make docqa-ingest KEYS="raw/a.pdf"` | Backfill or re-run from your laptop (prints per-document results) |
| `make docqa-stats` | Chunks in the index |
| `aws logs tail /aws/lambda/docqa-dev-ingest --follow` | Watch ingestion (logs carry IDs and counts, never document text) |

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
