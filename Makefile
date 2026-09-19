.PHONY: help install mockapp discover replay operator test lint typecheck evidence clean

PY ?= python3
MOCKAPP_PORT ?= 8000

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install: ## Install the package and dev extras, plus Chromium
	$(PY) -m pip install -e ".[dev]"
	$(PY) -m playwright install chromium

mockapp: ## Serve the mock legacy credit-union core on :8000
	$(PY) -m uvicorn mockapp.app:app --host 127.0.0.1 --port $(MOCKAPP_PORT) --reload

discover: ## Goal-driven discovery run (M2)
	$(PY) -m cua.cli discover $(ARGS)

replay: ## Deterministic replay of a capability (M4)
	$(PY) -m cua.cli replay $(ARGS)

operator: ## Operator console for handoff (M6)
	$(PY) -m cua.cli operator $(ARGS)

test: lint typecheck ## Lint, typecheck, then run the test suite
	$(PY) -m pytest

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

typecheck:
	$(PY) -m mypy

evidence: ## Regenerate the replay evidence (needs :8000 free); discovery runs are kept
	$(PY) -m cua.evidence.build

clean:
	rm -rf .pytest_cache .ruff_cache .mypy_cache
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
