.PHONY: setup up down test lint typecheck eval redteam demo fmt

VENV := .venv
BIN  := $(VENV)/bin

setup:  ## create venv and install all packages in editable mode
	uv venv --python 3.11 $(VENV)
	uv pip install --python $(BIN)/python \
		-e packages/core -e packages/airwallex -e packages/agent \
		-e packages/audit -e packages/evalkit -e apps/api -e apps/worker \
		pytest pytest-cov hypothesis ruff mypy pre-commit import-linter types-PyYAML

up:  ## start postgres, api, worker, web (services land on Day 11+)
	docker compose up -d

down:
	docker compose down

test:  ## unit tests (integration/e2e are opt-in by marker or path)
	$(BIN)/python -m pytest tests/unit

lint:  ## ruff; import-linter contracts are added on Day 13
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:  ## mypy strict on tessera_core, standard on the rest
	$(BIN)/mypy packages/core/tessera_core packages/airwallex/tessera_airwallex packages/agent/tessera_agent

eval:  ## forecast and savings evaluation (Day 17)
	@echo "eval: not implemented yet (Day 17)"

redteam:  ## adversarial suite (Day 10 / Day 18)
	@echo "redteam: not implemented yet (Day 10/18)"

demo:  ## end-to-end scripted demo (Day 19)
	@echo "demo: not implemented yet (Day 19)"
