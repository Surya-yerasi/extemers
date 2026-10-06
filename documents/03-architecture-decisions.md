# Architecture decisions: platform

Lightweight ADRs: the decision, the alternatives, why it won, and what it costs. When a decision
changes, add a record that supersedes the old one. Service-level decisions (runtime, packaging,
API style, framework) live in each service's own document.

Records for the removed calculator service (Python/Lambda runtime, zip packaging, API Gateway HTTP API,
Powertools, Pydantic, Decimal arithmetic, local-dev tooling) were retired with it and remain in git
history.

## Index

| # | Area | Decision |
|---|---|---|
| [P-01](#p-01-terraform) | IaC | Terraform |
| [P-02](#p-02-module--per-environment-roots) | IaC | Reusable modules + one root per environment |
| [P-03](#p-03-s3-state-with-native-locking) | IaC | S3 state with native lock file |
| [P-04](#p-04-separate-bootstrap-stack) | IaC | Separate bootstrap stack with local state |
| [P-05](#p-05-github-actions) | CI/CD | GitHub Actions |
| [P-06](#p-06-oidc-with-two-roles) | CI/CD | OIDC federation with separate plan and deploy roles |
| [P-07](#p-07-immutable-oidc-subject) | CI/CD | Trust GitHub's immutable OIDC subject format |
| [P-08](#p-08-deploy-after-successful-ci-on-main) | CI/CD | Deploy runs after CI succeeds on `main` |
| [P-09](#p-09-removals-go-through-the-pipeline) | CI/CD | Removing infrastructure goes through the same PR → plan → apply path |
| [P-10](#p-10-tagging-strategy) | Ops | `default_tags` with a `project` umbrella tag |
| [P-11](#p-11-makefile-as-the-single-interface) | Tooling | Makefile as the single command interface |

---

## P-01: Terraform

| Option | For | Against |
|---|---|---|
| **Terraform** ✅ | Widely used in large enterprises; explicit plan/apply; large module ecosystem; multi-cloud skills transfer | You manage state (solved by P-03) |
| AWS CDK | Infrastructure in Python/TS | Synthesises CloudFormation; slower, opaque diffs |
| AWS SAM | Serverless-focused | CloudFormation underneath; awkward beyond serverless |
| CloudFormation | Native, no state | Verbose; slower feedback |
| Pulumi | Real languages | Smaller enterprise footprint |

## P-02: Module + per-environment roots

| Option | For | Against |
|---|---|---|
| **Modules + directory per env** ✅ | Explicit, reviewable environments; separate state per env | Some repetition between env directories |
| Terraform workspaces | Less duplication | Easy to apply to the wrong workspace; discouraged for env separation |
| Terragrunt | DRY across many envs/accounts | Another tool; overkill at this size |

## P-03: S3 state with native locking

| Option | For | Against |
|---|---|---|
| **S3 + native lock file** ✅ | Terraform ≥ 1.10 locks with a `.tflock` object; no DynamoDB; versioned for rollback | Needs Terraform ≥ 1.10 |
| S3 + DynamoDB lock table | Long-standing pattern | Extra table; now deprecated in favour of native locking |
| HCP Terraform | Managed runs and policy | External SaaS; often needs security approval in banks |
| Local state | Zero setup | Cannot be shared with CI |

## P-04: Separate bootstrap stack

| Option | For | Against |
|---|---|---|
| **Separate root, human-applied** ✅ | Solves the chicken-and-egg problem of the state bucket; CI never holds power over its own permissions | Its local state file must be kept safe (or migrated into the bucket) |
| Click-ops | Fast | Not reproducible or reviewable |
| Same root as services | One apply | CI could edit its own IAM roles, a privilege-escalation path |

## P-05: GitHub Actions

| Option | For | Against |
|---|---|---|
| **GitHub Actions** ✅ | Code lives in GitHub; native OIDC; PR comments; environments with approvals | Hosted runners can have outages (hit on 2026-10-05) |
| CodePipeline / CodeBuild | Inside AWS, IAM-native | More resources; weaker PR integration |
| Jenkins | Common in enterprises | Servers to run and patch |

## P-06: OIDC with two roles

![OIDC flow](diagrams/04-oidc-auth.png)

| Option | For | Against |
|---|---|---|
| **OIDC, plan role + deploy role** ✅ | No secrets; ≤ 1 h credentials; PR code can never write; deploys can be gated by environment reviewers | Slightly more set-up |
| OIDC, one role | Simpler | Any PR could apply with write access |
| Access keys in GitHub Secrets | Fastest set-up | Long-lived secrets; usually forbidden by enterprise policy |

## P-07: Immutable OIDC subject

**Context.** The first plan run failed with `Not authorized to perform sts:AssumeRoleWithWebIdentity`.
This repository has GitHub's `use_immutable_subject` enabled, so tokens carry
`repo:Surya-yerasi@61959369/extemers@1405205156:…`, while the trust policies expected
`repo:Surya-yerasi/extemers:…`.

**Decision.** Trust the immutable format, through the `github_subject_prefix` variable.

| Option | For | Against |
|---|---|---|
| **Immutable subject only** ✅ | Bound to numeric owner and repo IDs; a renamed or re-created same-name repo cannot match | Prefix must be looked up once (`gh api …/oidc/customization/sub`) |
| Name-based subject only | Readable | Broken for this repo; vulnerable to repo-name reuse |
| Accept both formats | Works whatever the repo setting | Keeps the name-reuse weakness |

## P-08: Deploy after successful CI on `main`

`deploy.yml` runs on `workflow_run` when CI completes **successfully** for a **push** to `main`, or
by manual dispatch. `concurrency: deploy-dev` (no cancel) prevents overlapping applies.

| Option | For | Against |
|---|---|---|
| **Separate workflow after CI** ✅ | Only commits that passed CI deploy; CI and deploy permissions stay separate | `workflow_run` only fires once the file is on the default branch |
| Deploy job inside `ci.yml` | One file | Shared triggers and permissions |
| Manual deploys only | Control | Not continuous delivery |

## P-09: Removals go through the pipeline

**Decision.** Infrastructure is removed by deleting its module from the env root in a PR. The PR plan
must show only the expected destroys, and the merge applies them.

| Option | For | Against |
|---|---|---|
| **PR → plan → apply** ✅ | Reviewed; state stays consistent; audit trail in Git and CloudTrail | Takes a PR cycle |
| Local `terraform destroy` | Fast | Unreviewed; bypasses the deploy role's scoping |
| Console deletion / `nuke_orphans.sh` | Works without state | State drifts; last resort only |

**Consequence.** Permission changes that would remove the deploy role's access to a prefix must wait
until that prefix's resources are destroyed.

## P-10: Tagging strategy

| Option | For | Against |
|---|---|---|
| **`default_tags` + `project` umbrella + module `app`** ✅ | Every taggable resource tagged automatically; supports billing, Resource Groups and tag-based cleanup | A few resource types are untaggable or invisible to the tagging API |
| Name prefixes only | Easy in the console | Billing and the tagging API cannot use names |
| Per-resource `tags` blocks | Explicit | Easy to forget |

Bootstrap is excluded from `project`, so tag sweeps cannot delete the state bucket or CI roles.

## P-11: Makefile as the single interface

| Option | For | Against |
|---|---|---|
| **Make** ✅ | Preinstalled; CI steps are one-liners; `make help` | Tab-sensitive; complex logic belongs in scripts |
| `just` / Taskfile | Nicer syntax | Another tool to install |
| Raw commands in docs and CI | No extra file | Local and CI commands drift apart |
