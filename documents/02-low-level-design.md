# Low-level design: platform

## Repository layout

```
extemers/
├── infra/
│   ├── bootstrap/        one-time account setup (local state, human-applied)
│   ├── modules/          reusable modules, added per service
│   └── envs/dev/         dev environment root (S3 state)
├── services/             one directory per service, added as built (next: docqa)
├── scripts/              audit_tags.sh, nuke_orphans.sh
├── .github/workflows/    ci.yml, deploy.yml
├── documents/            this documentation
├── Makefile              single entry point for every command
└── .pre-commit-config.yaml
```

![Infrastructure](diagrams/02-infrastructure.png)

<sub>Source: [diagrams/02-infrastructure.mmd](diagrams/02-infrastructure.mmd)</sub>

## `infra/bootstrap`

**Variables**

| Variable | Default | Purpose |
|---|---|---|
| `region` | `us-east-1` | |
| `github_subject_prefix` | `repo:Surya-yerasi@61959369/extemers@1405205156` | Prefix of the OIDC `sub` claim. GitHub's immutable subject format, validated to start with `repo:` |
| `deploy_environment` | `dev` | GitHub environment whose jobs may assume the deploy role |
| `managed_name_prefixes` | `["calculator-"]` → becomes `["docqa-"]` | Name prefixes the deploy role may manage |
| `create_oidc_provider` | `true` | Set `false` if the account already has the GitHub provider |

**Resources**

| Resource | Purpose |
|---|---|
| `aws_s3_bucket.state` (`extemers-tfstate-<account>`) | Remote state; `prevent_destroy = true` |
| `…_versioning`, `…_server_side_encryption_configuration`, `…_public_access_block`, `…_policy` | Versioned, AES-256, fully private, denies non-TLS requests |
| `aws_iam_openid_connect_provider.github` | Trusts `token.actions.githubusercontent.com`, audience `sts.amazonaws.com` |
| `aws_iam_role.plan` (`extemers-github-plan`) | Trust: `sub = <prefix>:pull_request`. AWS `ReadOnlyAccess` + state bucket access |
| `aws_iam_role.deploy` (`extemers-github-deploy`) | Trust: `sub = <prefix>:environment:dev`. Permissions below |

**Deploy role permissions** (scoped by `managed_name_prefixes`):

| Statement | Actions | Resources |
|---|---|---|
| State | `s3:ListBucket`, `Get/Put/DeleteObject` | The state bucket |
| `Lambda` | `lambda:*` | `function:<prefix>*` |
| `LambdaExecutionRoles` | Create, update, delete and tag roles; inline policies | `role/<prefix>*` |
| `PassRoleToLambdaOnly` | `iam:PassRole` with `iam:PassedToService = lambda.amazonaws.com` | `role/<prefix>*` |
| `ApiGateway` | `GET/POST/PUT/PATCH/DELETE`, tag/untag | `/apis`, `/apis/*`, `/tags/*` in the region |
| `LogGroups` | `logs:*` | `/aws/lambda/<prefix>*`, `/aws/apigateway/<prefix>*` |
| `LogsAccountLevel` | Describe/list and log-delivery actions | `*` (these actions do not support resource scoping) |

The permission set is revised per service. docqa will add ECR, S3 (its docs bucket), Cognito, SSM
and Budgets, and drop API Gateway.

**Outputs:** `state_bucket`, `plan_role_arn`, `deploy_role_arn`.

## `infra/envs/dev`

| File | Contents |
|---|---|
| `versions.tf` | Terraform ≥ 1.10, AWS provider `~> 6.0`, partial `s3` backend (`key`, `encrypt`, `use_lockfile`), provider `default_tags` |
| `variables.tf` | `region` (`us-east-1`), `owner` |
| `main.tf` | Service module calls. Currently none; removing a module block destroys its resources on the next apply |
| `backend.hcl.example` | Template for the git-ignored `backend.hcl` (bucket, region) |

The backend config is partial so no account ID is committed. CI passes `-backend-config` flags, and
local runs use `backend.hcl`.

## Tags

`default_tags` add `project=extemers`, `env`, `owner`, `managed_by=terraform` and `repository` to every
taggable resource. Each service module sets `app=<service>`. Bootstrap uses only
`app=extemers-bootstrap` and `managed_by`, deliberately without `project`, so tag sweeps never touch
shared plumbing.

## Scripts

| Script | Behaviour |
|---|---|
| `scripts/audit_tags.sh` | Read-only. Lists ARNs tagged `project=<PROJECT_TAG>` (default `extemers`) in `AWS_REGION` via the Resource Groups Tagging API |
| `scripts/nuke_orphans.sh` | Dry run by default. With `--yes` and typed confirmation, deletes tagged Lambda functions, HTTP APIs (stage ARNs mapped back to the API ID) and log groups; lists anything else for manual deletion |

## Makefile targets

| Target | Runs |
|---|---|
| `install` | `pre-commit install` |
| `tf-fmt` / `tf-validate` | `terraform fmt -check`; `init -backend=false` + `validate` for bootstrap, every module and every env |
| `tf-init` / `tf-plan` / `tf-apply` / `tf-destroy` | Against `infra/envs/$(ENV)` (default `dev`) |
| `audit-tags` / `nuke-orphans` | Tag listing / dry-run cleanup |
| `docs-diagrams` | Re-render `documents/diagrams/*.mmd` to PNG |
| `clean` | Remove local build and cache directories |

Service-specific targets (lint, test, build, run) are added with each service.

## Pre-commit hooks

Trailing whitespace, end-of-file, YAML check, large files (> 500 KB), AWS credential detection,
private-key detection, gitleaks, and `terraform fmt`. Each service adds its own language hooks.
