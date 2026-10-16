PYTHON ?= python3.12
VENV   ?= .venv
BIN    := $(VENV)/bin
OLLAMA_MODEL ?= $(shell grep -E '^OLLAMA_MODEL=' .env 2>/dev/null | cut -d= -f2 || true)
OLLAMA_MODEL := $(or $(OLLAMA_MODEL),llama3.1:8b)

.PHONY: install dev test test-integration lint format pull-model up down

install:  ## Create the virtualenv and install pinned dev dependencies
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements-dev.txt

dev:  ## Run the API with auto-reload (reads .env)
	$(BIN)/uvicorn --factory app.main:create_app --reload --host 127.0.0.1 --port 9000 \
		--loop uvloop --http httptools --no-access-log

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

pull-model:  ## Pull the model into the compose Ollama, or a local one if compose isn't running
	docker compose exec ollama ollama pull $(OLLAMA_MODEL) 2>/dev/null || ollama pull $(OLLAMA_MODEL)

up:
	docker compose up -d --build

down:
	docker compose down
