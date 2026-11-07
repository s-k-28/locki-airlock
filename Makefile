# LOCKED IN. Run `make help` for targets.
PORT ?= 8077
SPACETIME ?= $(shell command -v spacetime 2>/dev/null || echo "$(HOME)/.local/bin/spacetime")

.PHONY: help setup models verify-models backend frontend dev demo test lint bench clean-data soak build offline-check hardware airlock airlock-build airlock-verify airlock-cloud

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

airlock: ## Start AIRLOCK's real SpacetimeDB and security dashboard; no models or robot commands
	AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh

airlock-cloud: ## Run the dashboard against the team's existing locki-w91bw cloud deployment
	AIRLOCK_SPACETIME_URI=https://maincloud.spacetimedb.com AIRLOCK_DATABASE=locki-w91bw AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh frontend

airlock-build: ## Publish the local AIRLOCK module without deleting data and build the dashboard
	AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh build

airlock-verify: ## Exercise the deployed AIRLOCK module using separate authenticated clients
	AIRLOCK_SPACETIME_BIN="$(SPACETIME)" bash scripts/airlock_dev.sh verify

setup: ## Install backend (Python 3.12 via uv) and frontend deps, download models
	cd backend && uv sync --all-extras --python 3.12
	cd frontend && npm install --no-fund --no-audit
	$(MAKE) models

models: ## Download the pinned models and check them against backend/models.sha256 (needs internet once)
	cd backend && uv run python scripts/download_models.py

verify-models: ## Check the downloaded models against backend/models.sha256 (offline)
	cd backend && .venv/bin/python scripts/download_models.py --verify

backend: ## Backend only, auto-reload, http://127.0.0.1:$(PORT)
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload

frontend: ## Dashboard dev server, http://127.0.0.1:5174 (proxies to the backend)
	cd frontend && npm run dev

dev: ## Backend + dashboard dev server together (Ctrl-C stops both)
	@trap 'kill 0' INT TERM; \
	(cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port $(PORT) --reload) & \
	(cd frontend && npm run dev) & \
	wait

demo: ## Build the dashboard and serve everything from one process on :$(PORT)
	cd frontend && npm run build
	cd backend && uv run uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

# Real Zeus Car (docs/HARDWARE_HANDOFF.md). The venv's Python is used directly, not `uv run`,
# so nothing tries to resolve packages on the robot's Wi-Fi (no internet there).
# The robot's voice goes to SPEAKER (an output device name, e.g. make hardware SPEAKER="JLab GO Party");
# empty = the device saved in the dashboard settings.
ZEUS_HOST ?= 192.168.4.1
SPEAKER ?=
HW_ENV = LOCKEDIN_ROBOT_DRIVER=zeus LOCKEDIN_ZEUS_AUTOCONNECT=true LOCKEDIN_ZEUS_HOST=$(ZEUS_HOST) \
	LOCKEDIN_ZEUS_PORT=8765 LOCKEDIN_ZEUS_CAMERA_URL=http://$(ZEUS_HOST):9000/mjpg LOCKEDIN_CAMERA_SOURCE=zeus \
	LOCKEDIN_VISION_AUTOSTART=true LOCKEDIN_VOICE_AUTOSTART=true LOCKEDIN_OFFLINE=true \
	$(if $(SPEAKER),LOCKEDIN_AUDIO_OUTPUT="$(SPEAKER)")

build: frontend/dist/index.html ## Build the dashboard if its source changed (works offline)

frontend/dist/index.html: $(shell find frontend/src -type f) frontend/index.html frontend/package.json frontend/vite.config.ts
	cd frontend && npm run build

offline-check: ## Offline readiness: packages, models, dashboard, voice cache, audio, robot address (run on home Wi-Fi)
	cd backend && .venv/bin/python scripts/offline_check.py

hardware: build ## Real Zeus Car after joining its Wi-Fi: ESP32 camera, voice, monitor link, offline, disarmed, no motor commands
	@cd backend && $(HW_ENV) .venv/bin/python scripts/offline_check.py --no-smoke || { echo; echo "Fix the FAIL lines above first."; exit 1; }
	@echo; echo "LOCKED IN hardware mode -> http://127.0.0.1:$(PORT)   (Ctrl-C stops it; it never arms or moves the robot)"; echo
	cd backend && $(HW_ENV) .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port $(PORT)

test: ## Backend tests + frontend type-check/build
	cd backend && uv run pytest -q
	cd frontend && npm run build

lint: ## Ruff on the backend
	cd backend && uv run ruff check app tests scripts

bench: ## Reproduce docs/PERFORMANCE.md measurements (pass IMAGE=path/to/hands.jpg)
	cd backend && uv run python scripts/bench.py all $(if $(IMAGE),--image $(IMAGE),)

soak: ## Scripted demo + mixed sessions back to back on the simulated robot; checks invariants
	cd backend && uv run python scripts/soak_demo.py $(if $(DEMOS),--demos $(DEMOS),) $(if $(SESSIONS),--sessions $(SESSIONS),)

clean-data: ## Delete local telemetry, sessions and caches in data/ (asks first)
	@read -p "Delete everything in data/? [y/N] " ok && [ "$$ok" = "y" ] && rm -rf data/* && echo "data/ cleared"
