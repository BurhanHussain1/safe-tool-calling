# Task runner for bash / WSL / macOS. PowerShell users: see tasks.ps1
.DEFAULT_GOAL := help
PY := python

.PHONY: help setup lint fmt test check api assistant ui clean

help: ## Show available targets
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create a venv and install everything
	$(PY) -m venv .venv
	./.venv/bin/pip install --upgrade pip
	./.venv/bin/pip install -r requirements-dev.txt
	./.venv/bin/pip install -e .
	test -f .env || cp .env.example .env

lint: ## Lint
	ruff check .

fmt: ## Format and autofix
	ruff format .
	ruff check --fix .

test: ## Run the test suite (offline)
	pytest -q

check: lint ## Everything CI runs
	ruff format --check .
	pytest -q --cov=nl2api --cov-report=term-missing

api: ## Mock business API on :8000
	uvicorn nl2api.mock_api.main:app --reload --port 8000

assistant: ## Assistant API on :8001
	uvicorn nl2api.service.main:app --reload --port 8001

ui: ## Streamlit UI on :8501
	streamlit run src/nl2api/ui/app.py

clean: ## Remove caches and the local database
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	rm -f nl2api.db
