# Data Removal CLI — Makefile (Linux / macOS)
# Windows users: use dev.bat instead
.PHONY: setup test lint fmt run brokers browser-deps clean reset-db help

VENV := .venv
PYTHON := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
DR := $(VENV)/bin/dr

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

setup: $(VENV)/bin/activate  ## Full setup (venv + deps + install)
	@echo "✓ Ready — run: source $(VENV)/bin/activate"

$(VENV)/bin/activate:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip setuptools wheel -q
	$(PIP) install -e ".[dev]" -q
	@touch $(VENV)/bin/activate

browser-deps:  ## Install Playwright + Chromium for form automation
	$(PIP) install playwright
	$(VENV)/bin/playwright install chromium --with-deps

# ---------------------------------------------------------------------------
# Development
# ---------------------------------------------------------------------------

test:  ## Run tests
	$(PYTHON) -m pytest tests/ -v

test-watch:  ## Run tests on file change
	$(PIP) install pytest-watch -q
	$(VENV)/bin/ptw tests/

lint:  ## Type check + lint
	$(PIP) install mypy ruff -q
	$(VENV)/bin/ruff check dataremoval/ tests/
	$(VENV)/bin/mypy dataremoval/ --ignore-missing-imports

fmt:  ## Format code
	$(PIP) install ruff -q
	$(VENV)/bin/ruff format dataremoval/ tests/
	$(VENV)/bin/ruff check --fix dataremoval/ tests/

# ---------------------------------------------------------------------------
# CLI shortcuts
# ---------------------------------------------------------------------------

run:  ## Show CLI help
	$(DR) --help

brokers:  ## List supported brokers
	$(DR) brokers list

# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

clean:  ## Remove build artifacts and venv
	rm -rf $(VENV) *.egg-info dist build .pytest_cache .mypy_cache .ruff_cache
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

reset-db:  ## Delete the local database
	@$(PYTHON) -c "from dataremoval.core.database import default_db_path; p=default_db_path(); p.unlink(missing_ok=True); print(f'Deleted {p}')"

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'
