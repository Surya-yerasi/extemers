# extemers

A reference template for deploying AWS Lambda services (and later Bedrock agent/RAG services) the right way:
layered Python, strict quality gates, local runs, Terraform, and GitHub Actions with OIDC (no AWS keys).

The first service is a small calculator behind API Gateway:

```bash
curl -X POST "$API_URL/calculate" -d '{"operation":"add","a":2,"b":3}'
# {"operation":"add","a":"2","b":"3","result":"5"}
```

Operations are `add`, `subtract`, `multiply` and `divide`. Numbers are returned as decimal strings so
precision is exact (`0.1 + 0.2 = "0.3"`). Errors: `400` for divide-by-zero or invalid JSON, `422` for
schema violations, `404` for unknown routes. `GET /health` is a liveness check.

## Layout

```
src/calculator/
  domain.py     pure business logic, no AWS/HTTP imports, trivially unit-testable
  models.py     Pydantic request/response contracts
  service.py    orchestration; where Bedrock / Knowledge Base clients will live
  handler.py    thin Lambda adapter (Powertools router, logging, tracing, error mapping)
  config.py     env-var settings (APP_*)
tests/unit/          domain, models, handler (driven by real API Gateway v2 events)
tests/integration/   smoke tests against a deployed URL (skipped unless API_URL is set)
scripts/             build_lambda.sh (reproducible zip), local_server.py
infra/bootstrap/     one-time account setup: state bucket, GitHub OIDC, CI roles
infra/modules/       reusable lambda_http_api module
infra/envs/dev/      the dev environment (S3 remote state)
.github/workflows/   ci.yml (PRs + main), deploy.yml (main → dev)
```

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (installs Python 3.13 for you)
- Terraform ≥ 1.10: `brew install hashicorp/tap/terraform`
- Docker (optional, for `make run-rie`)
- AWS CLI with credentials, only needed for bootstrap and manual deploys

## Local development

```bash
make install      # deps + pre-commit hooks
make check        # ruff, mypy --strict, pytest (coverage gate 90%)
make run-local    # http://127.0.0.1:8000 (same handler, wrapped in API GW v2 events)
make run-rie      # build the zip and run it in the official Lambda image on :9000
```

To call the function in the Lambda image (`make run-rie`):

```bash
curl -XPOST localhost:9000/2015-03-31/functions/function/invocations -d @tests/fixtures/apigw_event.json
```

## First-time AWS setup (once per account)

1. Sign in: `aws login` (or `aws sso login`), then confirm with `aws sts get-caller-identity`.
2. Bootstrap. It uses local state, so keep `terraform.tfstate` somewhere safe:
   ```bash
   cd infra/bootstrap
   terraform init && terraform apply
   # Already have a GitHub OIDC provider in the account? Add -var create_oidc_provider=false
   ```
3. In GitHub, go to **Settings → Environments** and create `dev`. You can add required reviewers here
   for manual approval.
4. In **Settings → Secrets and variables → Actions → Variables**, add these from the bootstrap outputs:

   | Variable | Scope | Value |
   |---|---|---|
   | `TF_STATE_BUCKET` | repository | `state_bucket` |
   | `AWS_PLAN_ROLE_ARN` | repository | `plan_role_arn` |
   | `AWS_DEPLOY_ROLE_ARN` | environment `dev` | `deploy_role_arn` |
   | `AWS_REGION` | repository (optional) | `us-east-1` |

## CI/CD flow

| Trigger | What runs |
|---|---|
| Pull request | lint, typecheck, tests, build zip, `terraform fmt`/`validate`, then `terraform plan` posted as a PR comment (read-only role) |
| Push to `main` | CI, then **Deploy**: build, plan, apply to `dev` (deploy role, `dev` environment), then smoke tests |
| Manual | Run **Deploy** from the Actions tab |

The two IAM roles trust only this repo. The plan role accepts only `pull_request` tokens and is read-only.
The deploy role accepts only jobs running in the `dev` environment, and it can only manage resources
whose names start with `managed_name_prefixes` (default `calculator-`).

## Manual deploy / teardown

```bash
cp infra/envs/dev/backend.hcl.example infra/envs/dev/backend.hcl   # fill in the bucket
make tf-init && make tf-plan && make tf-apply
make smoke
make tf-destroy        # dev costs nothing while idle, but this removes it entirely
```

## Tagging and cleanup

Every resource created by `infra/envs/*` carries a fixed set of tags via the provider's
`default_tags` ([infra/envs/dev/versions.tf](infra/envs/dev/versions.tf)), applied automatically —
no per-resource tagging needed:

| Tag | Value | Purpose |
|---|---|---|
| `project` | `extemers` | Umbrella tag for the whole repo. Safe to delete everything carrying this. |
| `app` | `calculator` | This specific service, once more live under the same project. |
| `env` | `dev` | Environment. |
| `managed_by` | `terraform` | Marks it as IaC-managed, not clicked-together. |
| `repository` | `Surya-yerasi/extemers` | Where the code lives. |

Resource **names** also carry the `calculator-dev` prefix (the Lambda function, both CloudWatch
log groups, the API) — names are for scanning the console, tags are for programmatic cleanup and
billing. `infra/bootstrap` (the state bucket, OIDC provider, IAM roles) is deliberately **not**
tagged `project=extemers`: it's account-level plumbing meant to outlive any single app and must
never be caught by a tag-based deletion sweep.

**Day to day:** `make tf-destroy` is the normal way to tear down `dev`. It's driven by Terraform
state and is the only path that also cleans up IAM roles correctly.

**Safety net (independent of Terraform state):**

```bash
make audit-tags        # list everything tagged project=extemers, via the AWS Resource
                        # Groups Tagging API — the same source Billing uses for cost tags
make nuke-orphans       # dry run: shows what a tag-based sweep would delete
./scripts/nuke_orphans.sh --yes   # actually deletes (asks you to type the tag to confirm)
```

Use `nuke-orphans` only if `terraform destroy` can't run (e.g. lost or corrupted state) or to
double-check destroy actually got everything. It only deletes resource types the script knows how
to delete (Lambda, API Gateway, log groups today); anything else it lists but leaves for you.

**See costs by project:** tags only show up in Cost Explorer / CUR after you activate them as
**cost allocation tags** — one-time, in **Billing and Cost Management → Cost allocation tags** —
activate `project`, `app`, and `env`. Takes up to 24h to start populating. After that you can filter
Cost Explorer by `project: extemers` to see exactly what this template costs.

**See resources by project (no billing needed):** create a free **Resource Group** in the console
(Resource Groups & Tag Editor → Create group → tag filter `project = extemers`) for a live
dashboard of everything, updated in real time, independent of the 24h billing delay.

## Adding a new service (e.g. a Bedrock agent)

1. Create `src/<service>/` with the same `domain / models / service / handler / config` split.
   Put Bedrock calls in `service.py` behind a small interface so unit tests can stub them.
2. Add tests in `tests/unit/`.
3. Make `scripts/build_lambda.sh` take the package name as a parameter (today it copies `calculator`).
4. Add a `module "<service>"` block in `infra/envs/dev/main.tf` that uses `modules/lambda_http_api`. Pass
   IAM for Bedrock through `additional_policy_json` (e.g. `bedrock:InvokeModel` on specific model ARNs).
   Raise `timeout`, because LLM calls are slow and API Gateway caps integrations at 30s.
5. Add `"<service>-"` to `managed_name_prefixes` in bootstrap and re-apply it.
6. Don't add per-resource tags — the module/stack-level `default_tags` already cover the new
   service with `project=extemers`. Just update the `app` tag value if you give it its own env block.
