# Single entry point for developers and CI. `make help` lists targets.
ENV ?= dev
TF_DIR := infra/envs/$(ENV)
TF ?= terraform
DOCQA := services/docqa

.DEFAULT_GOAL := help
.PHONY: help install tf-fmt tf-validate tf-init tf-plan tf-apply tf-destroy \
        docqa-install docqa-check docqa-build docqa-run docqa-dev \
        docqa-samples docqa-upload-samples docqa-ingest docqa-stats docqa-ask \
        docqa-local-models docqa-local-samples docqa-local-ingest docqa-local-stats \
        docqa-local-ask docqa-local-dev \
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

tf-plan: ## Plan ENV; needs IMAGE_TAG=<git sha already in ECR>
	$(TF) -chdir=$(TF_DIR) plan -input=false -out=tfplan -var image_tag=$(IMAGE_TAG)

tf-apply: ## Apply the saved plan
	$(TF) -chdir=$(TF_DIR) apply -input=false tfplan

tf-destroy: ## Tear down ENV (the docs bucket and KMS key are protected by prevent_destroy)
	$(TF) -chdir=$(TF_DIR) destroy -var image_tag=$(IMAGE_TAG)

# ---------------------------------------------------------------- docqa
DOCQA_BUCKET = docqa-dev-$(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)

docqa-install: ## Install docqa dependencies
	cd $(DOCQA) && uv sync --frozen

docqa-check: ## docqa: ruff, mypy --strict, pytest with coverage gate
	cd $(DOCQA) && uv run ruff check . && uv run ruff format --check . && \
	  uv run mypy src tests && uv run pytest --cov --cov-report=term-missing

docqa-build: ## Build the docqa arm64 image as docqa:local
	docker buildx build --platform linux/arm64 --provenance=false --sbom=false \
	  -t docqa:local --load $(DOCQA)

docqa-run: docqa-build ## Run the image on :8080 against the deployed dev config (uses your AWS login)
	@mkdir -p build && aws configure export-credentials --format env-no-export > build/.aws-env
	docker run --rm -p 8080:8080 --env-file build/.aws-env -e AWS_REGION=us-east-1 \
	  -e DOCQA_ENVIRONMENT=local -e DOCQA_CONFIG_PARAMETER=/docqa/dev/config docqa:local; \
	  rm -f build/.aws-env

docqa-dev: ## Run docqa with auto-reload (no container) against the deployed dev config
	cd $(DOCQA) && DOCQA_CONFIG_PARAMETER=/docqa/dev/config AWS_REGION=us-east-1 \
	  uv run uvicorn docqa.web.app:create_app --factory --reload --port 8080

docqa-samples: ## Regenerate the synthetic sample documents
	cd $(DOCQA) && uv run python scripts/make_synthetic_docs.py

docqa-upload-samples: ## Upload the synthetic documents to raw/samples/ (S3 then triggers ingestion)
	aws s3 cp $(DOCQA)/samples/synthetic/ s3://$(DOCQA_BUCKET)/raw/samples/ --recursive

docqa-ingest: ## Ingest from your laptop: all of raw/, or KEYS="raw/a.pdf raw/b.png"
	cd $(DOCQA) && DOCQA_DOCS_BUCKET=$(DOCQA_BUCKET) AWS_REGION=us-east-1 uv run python -m docqa.cli ingest $(KEYS)

docqa-stats: ## Number of chunks in the index
	cd $(DOCQA) && DOCQA_DOCS_BUCKET=$(DOCQA_BUCKET) AWS_REGION=us-east-1 uv run python -m docqa.cli stats

docqa-ask: ## Ask from your laptop: make docqa-ask Q="What was my GPA?" [STRATEGY=dense|bm25|hybrid|hybrid_rerank]
	cd $(DOCQA) && DOCQA_DOCS_BUCKET=$(DOCQA_BUCKET) AWS_REGION=us-east-1 \
	  uv run python -m docqa.cli ask "$(Q)" --strategy $(or $(STRATEGY),hybrid)

# ---------------------------------------------------------------- docqa local mode
# Ollama models, files and index under services/docqa/.data/ (git- and docker-ignored). $0.
LOCAL := DOCQA_PROVIDER=local
LOCAL_MODELS := qwen2.5:7b bge-m3 qwen2.5vl:7b

docqa-local-models: ## Local mode: check Ollama is running and pull any missing models
	@curl -fsS http://localhost:11434/api/version >/dev/null || \
	  { echo "Ollama is not running: brew install ollama && brew services start ollama"; exit 1; }
	@for m in $(LOCAL_MODELS); do ollama list | grep -q "^$$m " || ollama pull $$m; done
	@ollama list

docqa-local-samples: ## Local mode: copy the synthetic documents into .data/raw/samples/
	mkdir -p $(DOCQA)/.data/raw/samples && cp $(DOCQA)/samples/synthetic/* $(DOCQA)/.data/raw/samples/

docqa-local-ingest: ## Local mode: ingest .data/raw/ (put your own files there too), or KEYS="raw/a.pdf"
	cd $(DOCQA) && $(LOCAL) uv run python -m docqa.cli ingest $(KEYS)

docqa-local-stats: ## Local mode: number of chunks in the local index
	cd $(DOCQA) && $(LOCAL) uv run python -m docqa.cli stats

docqa-local-ask: ## Local mode: make docqa-local-ask Q="What was my GPA?" [STRATEGY=...]
	cd $(DOCQA) && $(LOCAL) uv run python -m docqa.cli ask "$(Q)" --strategy $(or $(STRATEGY),hybrid)

docqa-local-dev: ## Local mode: the web app on :8080 (real Cognito login, local models and index)
	cd $(DOCQA) && $(LOCAL) DOCQA_CONFIG_PARAMETER=/docqa/dev/config AWS_REGION=us-east-1 \
	  uv run uvicorn docqa.web.app:create_app --factory --reload --reload-dir src --port 8080

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
