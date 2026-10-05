# Single entry point for developers and CI. `make help` lists targets.
ENV ?= dev
TF_DIR := infra/envs/$(ENV)
TF ?= terraform

.DEFAULT_GOAL := help
.PHONY: help install lint format typecheck test check build run-local run-rie \
        tf-fmt tf-validate tf-init tf-plan tf-apply tf-destroy smoke \
        audit-tags nuke-orphans clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install deps and git hooks
	uv sync --frozen
	uv run pre-commit install

lint: ## Ruff lint + format check
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-fix lint and format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy --strict
	uv run mypy src tests

test: ## Unit tests with coverage gate
	uv run pytest -m "not integration" --cov --cov-report=term-missing --cov-report=xml

check: lint typecheck test ## Everything CI runs on Python

build: ## Build build/lambda.zip
	./scripts/build_lambda.sh

run-local: ## Serve the handler on http://127.0.0.1:8000
	uv run python scripts/local_server.py

run-rie: build ## Run the zip in the official Lambda runtime image on :9000
	rm -rf build/rie && mkdir -p build/rie && unzip -q build/lambda.zip -d build/rie
	docker run --rm -p 9000:8080 --platform linux/arm64 -v "$(CURDIR)/build/rie:/var/task:ro" \
	  -e POWERTOOLS_TRACE_DISABLED=true public.ecr.aws/lambda/python:3.13 calculator.handler.lambda_handler

tf-fmt: ## terraform fmt check
	$(TF) fmt -recursive -check infra

tf-validate: ## Validate every stack without a backend
	@for d in infra/bootstrap infra/modules/lambda_http_api infra/envs/*; do \
	  echo "== $$d"; $(TF) -chdir=$$d init -backend=false -input=false >/dev/null && $(TF) -chdir=$$d validate || exit 1; \
	done

tf-init: ## terraform init (needs infra/envs/$(ENV)/backend.hcl)
	$(TF) -chdir=$(TF_DIR) init -input=false -backend-config=backend.hcl

tf-plan: build ## Plan ENV (default dev)
	$(TF) -chdir=$(TF_DIR) plan -input=false -out=tfplan

tf-apply: ## Apply the saved plan
	$(TF) -chdir=$(TF_DIR) apply -input=false tfplan

tf-destroy: build ## Tear down ENV
	$(TF) -chdir=$(TF_DIR) destroy

smoke: ## Integration tests against deployed ENV
	API_URL=$$($(TF) -chdir=$(TF_DIR) output -raw api_url) uv run pytest -m integration -v

audit-tags: ## List everything tagged project=extemers (independent of TF state)
	./scripts/audit_tags.sh

nuke-orphans: ## Dry run only. To actually delete: ./scripts/nuke_orphans.sh --yes
	./scripts/nuke_orphans.sh

clean:
	rm -rf build .pytest_cache .mypy_cache .ruff_cache coverage.xml .coverage
