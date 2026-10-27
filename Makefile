PYTHON ?= python3.13
VENV   ?= .venv
BIN    := $(VENV)/bin
OLLAMA_MODEL ?= $(shell grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2 || true)
OLLAMA_MODEL := $(or $(OLLAMA_MODEL),llama3.1:8b)

.PHONY: install dev shell test test-integration lint format pull-model

install:  ## Create the virtualenv and install pinned dev dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

dev:  ## Run the API with auto-reload (reads .env)
	$(BIN)/uvicorn --factory app.main:create_app --reload --host 0.0.0.0 --port 9000 \
		--loop uvloop --http httptools --no-access-log

shell:  ## Interactive SQL shell to ask and teach the data agent (ARGS="--role rider --user-id 1771")
	$(BIN)/python -m app.cli.sql_shell $(ARGS)

test:  ## Unit/API tests (no Ollama or MySQL needed)
	$(BIN)/pytest

test-integration:  ## Against real Ollama + MySQL from .env
	$(BIN)/pytest -m integration

lint:
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:
	$(BIN)/ruff format .
	$(BIN)/ruff check --fix .

pull-model:  ## Pull OLLAMA_MODEL into the local Ollama
	ollama pull $(OLLAMA_MODEL)
