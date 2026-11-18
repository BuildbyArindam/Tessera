.PHONY: setup up down test test-integration db-test-up db-test-down migrate api lint typecheck eval redteam demo fmt seed web-client web-install web-dev web-check redteam-full agent-eval e2e-demo check-readme redteam-verify secrets ci

VENV := .venv
BIN  := $(VENV)/bin

setup:  ## create venv and install all packages in editable mode
	uv venv --python 3.11 $(VENV)
	uv pip install --python $(BIN)/python \
		-e packages/core -e packages/airwallex -e packages/agent \
		-e packages/store -e packages/audit -e packages/evalkit -e apps/api -e apps/worker \
		pytest pytest-cov hypothesis respx ruff==0.17.5 mypy==2.4.1 pre-commit import-linter types-PyYAML

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

redteam-full:  ## Day 18: 150+ attacks and 40+ benign cases through the real stack (needs `make db-test-up`); writes docs/redteam-report.md
	$(BIN)/python -m tessera_evalkit.redteam.suite --stack tessera_worker.redteam_stack:open_stack --out artifacts/redteam-full --report docs/redteam-report.md

agent-eval:  ## Day 19: agent vs treasurer labels (scripted planners; add `--planner llm` with LLM_API_KEY for a real model); writes docs/agent-eval-report.md
	$(BIN)/python -m tessera_evalkit.agent_eval $(AGENT_EVAL_ARGS)

e2e-demo:  ## Day 19: Playwright drives the same flow through the dashboard (needs `make db-test-up` and `make web-install`; set CHROME_PATH if Chromium is not installed)
	cd apps/web && npm run build && npm run test:e2e:day19

demo:  ## Day 19: seed, ingest a USD invoice, run the worker, escalate, approve, execute on the mock, webhook, verify the chain (needs `make db-test-up`)
	$(BIN)/python scripts/demo.py

check-readme:  ## Day 20: every number in README.md appears in a report under docs/ (or is allowlisted with a reason)
	$(BIN)/python scripts/check_readme_numbers.py

redteam-verify:  ## Day 20: re-run the full red-team suite into artifacts/ and fail if docs/redteam-report.md would change (needs a database)
	$(BIN)/python -m tessera_evalkit.redteam.suite --stack tessera_worker.redteam_stack:open_stack --out artifacts/redteam-full --report artifacts/redteam-report.md
	diff docs/redteam-report.md artifacts/redteam-report.md

secrets:  ## Day 20: gitleaks over the working tree and the full git history (needs the gitleaks binary on PATH)
	gitleaks detect --source . --config .gitleaks.toml --redact --verbose

ci:  ## the commands CI runs, in order. Needs a database: `make db-test-up`, or set TEST_DATABASE_ADMIN_URL
	$(MAKE) lint typecheck test test-integration check-readme redteam-verify redteam demo
