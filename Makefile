# ProspectPilot developer commands. `make help` lists them.
SHELL := /bin/bash
UV ?= uv
PP := $(UV) run prospectpilot
COMPOSE := docker compose
# provider for eval/improve on the host: mock | ollama | anthropic | openai
PROVIDER ?= ollama

.PHONY: help install hooks up down logs ps migrate demo test test-int lint fmt typecheck check eval eval-smoke improve results tf-validate clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}'

install: ## install deps (uv) and git hooks
	$(UV) sync
	git config core.hooksPath .githooks

hooks: ## install the pre-commit hook (ruff + unit tests)
	git config core.hooksPath .githooks

up: ## build and start the full local stack (postgres, mailpit, jaeger, otel, prometheus, grafana, api, worker)
	$(COMPOSE) up -d --build --wait
	@echo "API      http://localhost:8000/docs"
	@echo "Mailpit  http://localhost:8025"
	@echo "Jaeger   http://localhost:16686"
	@echo "Grafana  http://localhost:3000"

down: ## stop the stack (keeps the database volume)
	$(COMPOSE) down

logs: ## tail api + worker logs
	$(COMPOSE) logs -f api worker

ps:
	$(COMPOSE) ps

migrate: ## apply migrations from the host
	$(PP) db upgrade

demo: ## run a full campaign on fixture data inside the stack (no API keys needed)
	$(COMPOSE) exec -T api prospectpilot demo

test: ## unit tests (no network, no docker)
	$(UV) run pytest

test-int: ## integration + e2e tests (needs `make up`)
	$(UV) run pytest -m integration

lint: ## ruff lint + format check
	$(UV) run ruff check .
	$(UV) run ruff format --check .

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

typecheck: ## mypy --strict on src/
	$(UV) run mypy

check: lint typecheck test ## everything CI runs (minus integration)

eval: ## run the EvalForge suite against the ACTIVE prompt (PROVIDER=ollama|anthropic|mock)
	LLM_PROVIDER=$(PROVIDER) $(PP) eval

eval-smoke: ## tiny mock-mode eval used by CI
	LLM_PROVIDER=mock $(PP) eval --limit 8 --trials 1 --out evals/runs/scratch/smoke.json

improve: ## one self-improvement round (PROVIDER=ollama|anthropic)
	LLM_PROVIDER=$(PROVIDER) $(PP) improve

results: ## regenerate RESULTS.md from the database
	$(UV) run python scripts/make_results.py

tf-validate: ## terraform fmt/validate (never applies)
	cd infra/terraform && terraform init -backend=false -input=false >/dev/null && terraform fmt -check -recursive && terraform validate

clean:
	rm -rf .cache .pytest_cache .mypy_cache .ruff_cache
