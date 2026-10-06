# extemers

A lab for building GenAI services on AWS the way a regulated enterprise would: infrastructure as code,
a reviewed Terraform plan on every PR, keyless CI/CD through GitHub OIDC, and tagging for cost and
cleanup.

**Status:** the platform is in place and was proven end to end with a throw-away calculator Lambda,
since removed. The next service is **docqa**, a personal-document Q&A (RAG) app on Amazon Bedrock.

## Documentation

Platform design, architecture decisions, CI/CD and operations are in [documents/](documents/README.md),
with diagrams in [documents/diagrams/](documents/diagrams/).

## Layout

```
infra/bootstrap/     one-time account setup: state bucket, GitHub OIDC, CI roles (human-applied)
infra/modules/       reusable Terraform modules (added per service)
infra/envs/dev/      the dev environment root (S3 remote state)
services/            one directory per service (added as built)
scripts/             audit_tags.sh, nuke_orphans.sh
.github/workflows/   ci.yml (fmt/validate/plan), deploy.yml (apply on main)
documents/           design docs, ADRs, diagrams
```

## Quick start

```bash
make help            # every command
make install         # git hooks (needs: uv tool install pre-commit)
make tf-fmt tf-validate
make audit-tags      # list everything tagged project=extemers
```

First-time AWS setup, adding or removing a service, tagging and teardown are covered in
[documents/05-operations.md](documents/05-operations.md).
