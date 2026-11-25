.PHONY: help install-dev lock build up down logs ps lint format typecheck test check smoke root-cert importer importer-image

COMPOSE ?= docker compose
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
RUFF := $(PY) -m ruff
MYPY := $(PY) -m mypy
PYTEST := $(PY) -m pytest
UV := $(shell command -v uv 2>/dev/null)

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tr '\n' ' '; echo

# Local dev tools (no uv required)
install-dev:
	$(PY) -m pip install -e . pytest pytest-cov httpx2 ruff mypy types-PyYAML

# Lockfile for the Docker build. Requires uv; falls back to a printed instruction.
lock:
	@if [ -z "$(UV)" ]; then \
		echo "uv is not installed."; \
		echo "Install it: https://docs.astral.sh/uv/  (or: pip install uv)"; \
		echo "Alternative without uv:"; \
		echo "  $(PY) -m pip install pip-tools"; \
		echo "  $(PY) -m piptools compile --generate-hashes -o requirements.txt pyproject.toml"; \
		exit 1; \
	fi
	$(UV) lock
	$(UV) export --frozen --no-dev --no-emit-project --format requirements-txt -o requirements.txt

build:
	$(COMPOSE) build

up:
	$(COMPOSE) up -d

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=200

ps:
	$(COMPOSE) ps

lint:
	$(RUFF) check .
	$(RUFF) format --check .

format:
	$(RUFF) check --fix .
	$(RUFF) format .

typecheck:
	$(MYPY)

test:
	$(PYTEST)

check: lint typecheck test

smoke:
	./scripts/smoke_test.sh

root-cert:
	$(COMPOSE) cp proxy:/data/caddy/pki/authorities/local/root.crt ./caddy-root.crt
	@echo "Wrote ./caddy-root.crt - import it into client trust stores."

# Receiver-side importer as one file: dist/mirrorhub-importer.pyz (+ .sha256).
importer:
	$(PY) scripts/build_importer.py

# Docker image of the receiver importer with its web UI (Dockerfile.importer, docs/importer-ui.md).
importer-image:
	docker build -f Dockerfile.importer \
		--build-arg MIRRORHUB_COMMIT=$$(git rev-parse --short=12 HEAD 2>/dev/null) \
		-t mirrorhub-importer .
