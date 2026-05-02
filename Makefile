# Makefile for tbdtask — development, testing, and deployment tasks.
#
# Usage:
#   make test          — run the test suite
#   make lint          — run ruff (if installed)
#   make audit         — scan dependencies for known vulnerabilities
#   make freeze        — generate pinned requirements.txt
#   make build         — build the Docker image
#   make backup        — create a database backup
#   make restore       — restore from a backup file
#   make scan          — run all security scans (secrets, env, licenses)
#   make ci            — run full CI pipeline locally

SHELL := /bin/bash
.PHONY: test lint audit freeze build backup restore scan ci help

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_URL ?= sqlite:///data/tbdtask.db
BACKUP_DIR ?= data/backups
BACKUP_FILE ?= $(BACKUP_DIR)/tbdtask-$(shell date +%Y%m%d-%H%M%S).db

help:
	@echo "Available targets:"
	@echo "  test      — run the test suite"
	@echo "  lint      — run ruff linter (requires ruff)"
	@echo "  audit     — scan dependencies for known CVEs (requires pip-audit)"
	@echo "  freeze    — generate pinned requirements.txt from pyproject.toml"
	@echo "  build     — build the Docker image"
	@echo "  backup    — create a database backup"
	@echo "  restore   — restore from BACKUP_FILE=<path>"
	@echo "  scan      — run all security scans (secrets, env hygiene, licenses)"
	@echo "  ci        — run full CI pipeline locally (lint + test + scan)"
	@echo "  help      — show this help"

# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------

test:
	python3 -m pytest tests/ -v

# ---------------------------------------------------------------------------
# Linting
# ---------------------------------------------------------------------------

lint:
	@command -v ruff >/dev/null 2>&1 || { echo "ruff not installed: pip install ruff"; exit 1; }
	ruff check app/ tests/
	ruff format --check app/ tests/

# ---------------------------------------------------------------------------
# Dependency scanning (Phase 7)
# ---------------------------------------------------------------------------

audit:
	@command -v pip-audit >/dev/null 2>&1 || { echo "pip-audit not installed: pip install pip-audit"; exit 1; }
	pip-audit --require-hashes 2>/dev/null || pip-audit

freeze:
	@command -v pip-compile >/dev/null 2>&1 || { echo "pip-tools not installed: pip install pip-tools"; exit 1; }
	pip-compile pyproject.toml --output-file requirements.txt --generate-hashes

# ---------------------------------------------------------------------------
# Security scans
# ---------------------------------------------------------------------------

scan: scan-secrets scan-env scan-licenses

scan-secrets:
	python3 tools/scan_secrets.py app/ tools/ alembic/

scan-env:
	python3 tools/check_env_hygiene.py

scan-licenses:
	@command -v pip-licenses >/dev/null 2>&1 || { echo "pip-licenses not installed: pip install pip-licenses"; exit 1; }
	pip-licenses --format=json --output-file /tmp/tbdtask-licenses.json
	@python3 -c "\
	import json; \
	with open('/tmp/tbdtask-licenses.json') as f: licenses = json.load(f); \
	forbidden = {'GPL-3.0', 'GPL-2.0', 'AGPL-3.0', 'AGPL-2.0'}; \
	bad = [p for p in licenses if any(f in p.get('License', '') for f in forbidden)]; \
	[print(f'  Forbidden: {p[\"Name\"]}: {p[\"License\"]}') for p in bad]; \
	exit(1) if bad else print('No forbidden licenses found')"

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

build:
	docker build -t tbdtask:latest .

# ---------------------------------------------------------------------------
# Backup / Restore (Phase 7)
# ---------------------------------------------------------------------------

backup:
	@mkdir -p $(BACKUP_DIR)
	@# Extract the SQLite path from the DB_URL
	@DB_PATH=$$(echo "$(DB_URL)" | sed 's|sqlite:///||'); \
	if [ ! -f "$$DB_PATH" ]; then \
		echo "Database not found: $$DB_PATH"; \
		exit 1; \
	fi; \
	cp "$$DB_PATH" "$(BACKUP_FILE)" && \
	echo "Backup created: $(BACKUP_FILE)"

restore:
	@if [ -z "$(BACKUP_FILE)" ] || [ ! -f "$(BACKUP_FILE)" ]; then \
		echo "Backup file not found: $(BACKUP_FILE)"; \
		echo "Usage: make restore BACKUP_FILE=data/backups/tbdtask-20240101-120000.db"; \
		exit 1; \
	fi
	@DB_PATH=$$(echo "$(DB_URL)" | sed 's|sqlite:///||'); \
	cp "$(BACKUP_FILE)" "$$DB_PATH" && \
	echo "Restored from: $(BACKUP_FILE)"

# ---------------------------------------------------------------------------
# Full CI pipeline (local equivalent of GitHub Actions)
# ---------------------------------------------------------------------------

ci: lint test scan
	@echo "CI pipeline passed."
