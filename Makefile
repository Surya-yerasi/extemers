# Single entry point for developers and CI. `make help` lists targets.
# Service-specific targets (lint, test, build, run) live with each service under services/.
ENV ?= dev
TF_DIR := infra/envs/$(ENV)
TF ?= terraform

.DEFAULT_GOAL := help
.PHONY: help install tf-fmt tf-validate tf-init tf-plan tf-apply tf-destroy \
        audit-tags nuke-orphans docs-diagrams clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-14s %s\n", $$1, $$2}'

install: ## Install git hooks (needs pre-commit: `uv tool install pre-commit`)
	pre-commit install

tf-fmt: ## terraform fmt check
	$(TF) fmt -recursive -check infra

tf-validate: ## Validate every stack and module without a backend
	@for d in infra/bootstrap infra/modules/* infra/envs/*; do \
	  [ -d "$$d" ] || continue; \
	  echo "== $$d"; $(TF) -chdir=$$d init -backend=false -input=false >/dev/null && $(TF) -chdir=$$d validate || exit 1; \
	done

tf-init: ## terraform init (needs infra/envs/$(ENV)/backend.hcl)
	$(TF) -chdir=$(TF_DIR) init -input=false -backend-config=backend.hcl

tf-plan: ## Plan ENV (default dev)
	$(TF) -chdir=$(TF_DIR) plan -input=false -out=tfplan

tf-apply: ## Apply the saved plan
	$(TF) -chdir=$(TF_DIR) apply -input=false tfplan

tf-destroy: ## Tear down ENV
	$(TF) -chdir=$(TF_DIR) destroy

audit-tags: ## List everything tagged project=extemers (independent of TF state)
	./scripts/audit_tags.sh

nuke-orphans: ## Dry run only. To actually delete: ./scripts/nuke_orphans.sh --yes
	./scripts/nuke_orphans.sh

docs-diagrams: ## Render documents/diagrams/*.mmd to PNG (Docker)
	@for f in documents/diagrams/*.mmd; do \
	  echo "render $$f"; \
	  docker run --rm -v "$(CURDIR)/documents/diagrams:/data" minlag/mermaid-cli \
	    -i "/data/$$(basename $$f)" -o "/data/$$(basename $${f%.mmd}).png" -b white -s 2 >/dev/null || exit 1; \
	done

clean: ## Remove local build and cache directories
	rm -rf build .venv .pytest_cache .mypy_cache .ruff_cache coverage.xml .coverage
