# datagen-extract — user-facing Makefile
#
# Variables you can override on the command line, e.g.:
#   make run SCHEMA_DIR=samples/schemas ROWS=500 FORMAT=json
#
#   INPUT       requirements document fed to schema extraction
#   SCHEMA_DIR  where extracted/validated YAML schemas live
#   RUN_DIR     where a pipeline run writes its script + data
#   ROWS        base row count for root tables
#   FORMAT      output data format: csv | json | xml | parquet
#   SEED        random seed passed to the generated script

VENV       := .venv
PIP        := $(VENV)/bin/pip
PYTEST     := $(VENV)/bin/pytest
RUFF       := $(VENV)/bin/ruff
CLI        := $(VENV)/bin/datagen-extractor

INPUT      ?= input/requirements.md
SCHEMA_DIR ?= output
RUN_DIR    ?= run_output
ROWS       ?= 150
FORMAT     ?= csv
SEED       ?= 0

.DEFAULT_GOAL := help
.PHONY: help install setup generate-schema validate-schema graph pii \
        generate-code review run execute check test lint format clean

help: ## Show this help (default target)
	@echo "datagen-extract — synthetic data generation for banking systems"
	@echo ""
	@echo "Usage: make <target> [VAR=value ...]"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "Typical flows:"
	@echo "  automated:        make generate-schema && make run"
	@echo "  manual intercept: make generate-schema && make validate-schema \\"
	@echo "                    && make generate-code && make review && make execute"

install: ## Create the venv and install the package with dev dependencies
	python3 -m venv $(VENV)
	$(PIP) install -e ".[dev]"

setup: install ## install + verify the GitHub Copilot CLI is available
	@command -v copilot >/dev/null 2>&1 \
		&& echo "✓ Copilot CLI: $$(copilot --version | head -1)" \
		|| echo "✗ Copilot CLI not found — install it: https://docs.github.com/copilot/how-tos/copilot-cli"

generate-schema: ## Extract schemas from INPUT via Copilot CLI into SCHEMA_DIR
	$(CLI) extract $(INPUT) --output $(SCHEMA_DIR) --timeout 420

validate-schema: ## Validate every YAML schema in SCHEMA_DIR (Pydantic strict)
	$(CLI) validate $(SCHEMA_DIR)

graph: ## Show FK dependency graph and generation order for SCHEMA_DIR
	$(CLI) graph $(SCHEMA_DIR)

pii: ## Scan SCHEMA_DIR for PII columns; fails on untagged PII-like fields
	$(CLI) pii-scan $(SCHEMA_DIR)

generate-code: ## Pipeline up to code generation, then stop for review (manual intercept)
	$(CLI) generate $(SCHEMA_DIR) --out $(RUN_DIR) --rows $(ROWS) \
		--format $(FORMAT) --seed $(SEED) --timeout 420 --stop-after code

review: ## Print the generated script for review before executing it
	@cat $(RUN_DIR)/generated/generate_data.py

run: ## Full automated pipeline: plan -> codegen -> execute -> integrity checks
	$(CLI) generate $(SCHEMA_DIR) --out $(RUN_DIR) --rows $(ROWS) \
		--format $(FORMAT) --seed $(SEED) --timeout 420 --exec-timeout 420

execute: ## Execute the reviewed generated script, then run integrity checks
	$(CLI) execute $(RUN_DIR)/generated/generate_data.py --schema-dir $(SCHEMA_DIR) \
		--out $(RUN_DIR)/data --rows $(ROWS) --format $(FORMAT) --seed $(SEED)

check: ## Re-run integrity + fidelity checks on already-generated data
	$(CLI) check $(SCHEMA_DIR) $(RUN_DIR)/data

test: ## Run the full pytest suite (offline — uses a fake Copilot binary)
	$(PYTEST) tests/ -q

lint: ## Lint src/ and tests/ with ruff
	$(RUFF) check src tests

format: ## Auto-format src/ and tests/ with ruff
	$(RUFF) format src tests

clean: ## Remove run outputs and caches (keeps schemas and the venv)
	rm -rf $(RUN_DIR) run_credit .pytest_cache .ruff_cache
	find . -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
