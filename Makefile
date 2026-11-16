.PHONY: setup up down test test-integration db-test-up db-test-down migrate api lint typecheck eval redteam demo fmt seed web-client web-install web-dev web-check

VENV := .venv
BIN  := $(VENV)/bin

setup:  ## create venv and install all packages in editable mode
	uv venv --python 3.11 $(VENV)
	uv pip install --python $(BIN)/python \
		-e packages/core -e packages/airwallex -e packages/agent \
		-e packages/store -e packages/audit -e packages/evalkit -e apps/api -e apps/worker \
		pytest pytest-cov hypothesis respx ruff mypy pre-commit import-linter types-PyYAML

up:  ## start postgres (api, worker and web services land on Days 13 to 16)
	docker compose up -d --wait postgres

down:
	docker compose down

test:  ## unit tests and the Airwallex contract suite (no database needed)
	$(BIN)/python -m pytest tests/unit tests/contract

test-integration:  ## database and API tests: start a database first (make db-test-up), or set TEST_DATABASE_ADMIN_URL
	TESSERA_REQUIRE_DB=1 $(BIN)/python -m pytest tests/integration

db-test-up:  ## throwaway PostgreSQL 16 for the integration tests, on port 5433, kept in memory
	docker compose --profile test up -d --wait postgres-test

db-test-down:
	docker compose --profile test rm -sf postgres-test

migrate:  ## apply database migrations to DATABASE_URL
	$(BIN)/alembic upgrade head

api:  ## run the API on :8000 (needs DATABASE_URL and TESSERA_API_KEY; no plan-run provider is attached)
	$(BIN)/uvicorn --factory tessera_api.main:create_app --port 8000

PKG_PATHS := packages/core:packages/airwallex:packages/agent:packages/store:packages/audit:packages/evalkit:apps/api:apps/worker

lint:  ## ruff, then the import-linter architecture contracts (.importlinter)
	PYTHONPATH=$(PKG_PATHS) $(BIN)/lint-imports
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

fmt:
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:  ## mypy strict on tessera_core, standard on the rest
	$(BIN)/mypy packages/core/tessera_core packages/airwallex/tessera_airwallex packages/agent/tessera_agent \
		packages/store/tessera_store packages/audit/tessera_audit apps/api/tessera_api apps/worker/tessera_worker

seed:  ## fit the models on data/synthetic and load the demo business into DATABASE_URL (idempotent, about a minute)
	$(BIN)/python scripts/seed_demo.py

web-client:  ## regenerate apps/web/openapi.json and the typed TypeScript client from the FastAPI schema
	$(BIN)/python scripts/export_openapi.py
	cd apps/web && npm run gen:api

web-install:
	cd apps/web && npm install

web-dev:  ## dashboard on :3000 (needs TESSERA_API_URL and TESSERA_API_KEY for the server-side proxy; see apps/web/.env.example)
	cd apps/web && npm run dev

web-check:  ## the generated client is current, then typecheck, lint, unit tests and a production build
	$(BIN)/python scripts/export_openapi.py --check
	cd apps/web && npm run typecheck && npm run lint && npm test && npm run build

eval:  ## forecast and savings evaluation (Day 17)
	$(BIN)/python -m tessera_evalkit.evaluate --data data/synthetic --fx data/fx/fx_daily.parquet --out docs/eval --report docs/eval-report.md --seed 7
	$(BIN)/python scripts/verify_day17.py

redteam:  ## injection red-team suite: 25 cases through sanitiser, planner and policy (Day 10); exits 1 on a violation
	$(BIN)/python -m tessera_evalkit.redteam --out artifacts/redteam

demo:  ## end-to-end scripted demo (Day 19)
	@echo "demo: not implemented yet (Day 19)"
