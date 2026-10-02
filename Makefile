# Docker is the baseline: `make up` = Studio only (compose.yml). All-in-one GPU box: `make gpu-up` (compose.gpu-local.yml).
# Podman (legacy, gpu-local only): `make PODMAN=1 gpu-up`.
ifeq ($(PODMAN),1)
export HOST_UID := $(shell id -u)
export HOST_GID := $(shell id -g)
GPU_COMPOSE ?= podman-compose -f compose.gpu-local.yml -f compose.podman.yml
COMPOSE ?= docker compose
NODE_RUN = podman run --rm -v "$$PWD/web":/web:Z -w /web docker.io/library/node:22.20-slim
else
GPU_COMPOSE ?= docker compose -f compose.gpu-local.yml
COMPOSE ?= docker compose
NODE_RUN = docker run --rm -v "$$PWD/web":/web -w /web docker.io/library/node:22.20-slim
endif
PY = uv run
# Node mode (profile S) uses `!override`/`!reset` compose tags (Compose >= 2.24.4); podman-compose lacks them and would
# silently keep mounting ./models into Studio, so the nodes targets refuse PODMAN=1.
NODES = -f compose.gpu-local.yml -f compose.nodes.yml

.PHONY: help doctor build up down logs ps gpu-build gpu-up gpu-down gpu-logs gpu-ps models verify verify-full test lint web-build web-types \
        e2e test-process acceptance-cpu acceptance-gpu acceptance-gpu-nodes acceptance-offline lock-comfyui \
        runner-token up-nodes down-nodes switch-nodes

help:         ; @grep -E '^[a-z-]+:' Makefile | cut -d: -f1 | tr '\n' ' '; echo
doctor:       ; $(PY) assetstudio doctor
build:        ; $(COMPOSE) build
up:           ; $(COMPOSE) up -d
down:         ; $(COMPOSE) down
logs:         ; $(COMPOSE) logs -f --tail=200
ps:           ; $(COMPOSE) ps
# All-in-one GPU box (compose.gpu-local.yml: Studio + ComfyUI + aux + worker3d on this machine).
gpu-build:    ; $(GPU_COMPOSE) build
gpu-up:       ; $(GPU_COMPOSE) up -d
gpu-down:     ; $(GPU_COMPOSE) down
gpu-logs:     ; $(GPU_COMPOSE) logs -f --tail=200
gpu-ps:       ; $(GPU_COMPOSE) ps
# Explicit, resumable model download from config/models.lock.yaml. Never at container start.
models:       ; set -a; [ -f .env ] && . ./.env; set +a; uv run --script scripts/download_models.py $(ARGS)
verify:       ; $(PY) assetstudio models verify
verify-full:  ; $(PY) assetstudio models verify --full
lint:         ; $(PY) ruff check packages services/studio services/compute_node tests && $(PY) ruff check --target-version py310 --ignore UP046,UP047 services/worker3d
test:         ; $(PY) pytest -q
# Frontend: built in a Node container (no Node needed on the host).
web-build:    ; $(NODE_RUN) sh -c "npm ci --no-audit --no-fund && npx tsc -b --noEmit && npx vite build"
# Regenerate the committed OpenAPI schema + TS types (tests/unit/test_openapi_fresh.py fails when stale).
web-types:
	$(PY) assetstudio openapi --out web/src/lib/generated/openapi.json
	$(NODE_RUN) npm run gen:api
e2e: web-build
	uv run --group e2e python -m playwright install chromium && uv run --group e2e pytest tests/e2e -v -m e2e
# Studio, runner and fake engines as separate OS processes, SIGKILLed independently (tests/process, docs/acceptance.md).
test-process: ; $(PY) pytest tests/process -v -m process
acceptance-cpu: lint test test-process web-build e2e
# Real stack on the 2x4090 host: STRICT (a failed build or missing publication is a failure).
acceptance-gpu: ; STUDIO_URL=http://127.0.0.1:$${STUDIO_PORT:-8190} uv run pytest tests/gpu -v -s -m gpu
acceptance-gpu-nodes: ; STUDIO_URL=http://127.0.0.1:$${STUDIO_PORT:-8190} $(PY) pytest tests/gpu_nodes -v -s -m gpu_nodes
acceptance-offline: ; ./scripts/offline-check.sh
# Freeze ComfyUI's transitive deps from the built image into comfyui/constraints.txt.
lock-comfyui:
	$(GPU_COMPOSE) run --rm --no-deps comfyui pip freeze --exclude-editable \
	  | grep -v -E '^(torch|torchvision|torchaudio|nvidia-|triton)' > comfyui/constraints.txt
# worker3d unit tests (raw schema, UV rasterizer) inside the worker image, CPU tensors, no network.
test-worker3d: ; podman run --rm --network none -v "$$PWD":/src:ro,Z -w /src \
	-e PYTHONPATH=/src/services/worker3d:/src/services/worker_common:/src/packages/assetstudio_processing \
	localhost/assetstudio-worker3d:dev sh -c "python tests/worker3d/test_raw_raster.py && python tests/worker3d/test_geometry_policy.py"

# Node mode (docs/installation.md). Token file is gitignored under secrets/.
nodes-guard:
ifeq ($(PODMAN),1)
	@echo "node mode needs Docker Compose >= 2.24.4 (!override/!reset); not supported with PODMAN=1" >&2; exit 1
endif
	@v=$$($(COMPOSE) version --short 2>/dev/null | sed 's/^v//'); \
	  [ -n "$$v" ] && [ "$$(printf '%s\n2.24.4\n' "$$v" | sort -V | head -n1)" = "2.24.4" ] \
	  || { echo "node mode needs Docker Compose >= 2.24.4 (found: $${v:-none})" >&2; exit 1; }
runner-token: nodes-guard
	mkdir -p secrets && $(COMPOSE) -f compose.gpu-local.yml run --rm studio assetstudio runners group-create --name local --projects '*' --operations '*' 2>/dev/null || true; \
	$(COMPOSE) -f compose.gpu-local.yml run --rm studio assetstudio runners token --group local > secrets/runner_registration_token && chmod 600 secrets/runner_registration_token
up-nodes: nodes-guard ; $(COMPOSE) $(NODES) up -d
down-nodes: nodes-guard ; $(COMPOSE) $(NODES) down
# Waits until nothing is in flight, then records the mode; restart Studio with STUDIO_EXECUTION=nodes afterwards.
switch-nodes: nodes-guard ; $(COMPOSE) $(NODES) exec studio assetstudio execution switch --to nodes
