COMPOSE ?= podman-compose
.PHONY: build up down logs ps models verify smoke test lock-comfyui catalog-ids catalog-check web-build e2e e2e-gpu

build:        ; $(COMPOSE) build
up:           ; $(COMPOSE) up -d
down:         ; $(COMPOSE) down
logs:         ; $(COMPOSE) logs -f --tail=200
ps:           ; $(COMPOSE) ps
models:       ; ./scripts/download-models.sh
verify:       ; uv run --script scripts/verify-models.py
smoke:        ; ./scripts/smoke-test.sh
# Assign ids to new catalog entries (existing ids never change); check fails if any are missing.
catalog-ids:  ; uv run --with pyyaml python scripts/catalog-ids.py
catalog-check: ; uv run --with pyyaml python scripts/catalog-ids.py --check
test:         ; uv run --with pytest --with pyyaml --with trimesh==4.9.0 --with numpy --with pillow --with scipy --with networkx pytest tests/unit -q
# Freeze ComfyUI's transitive deps from the built image into comfyui/constraints.txt.
lock-comfyui:
	podman run --rm localhost/line-a-comfyui:dev pip freeze --exclude-editable \
	  | grep -v -E '^(torch|torchvision|torchaudio|nvidia-|triton)' > comfyui/constraints.txt

E2E_DEPS = --with pytest --with playwright==1.55.0 --with fastapi==0.121.0 --with uvicorn==0.38.0 --with httpx==0.28.1 \
  --with websockets==15.0.1 --with pyyaml==6.0.3 --with trimesh==4.9.0 --with numpy --with pillow --with scipy --with networkx
# Build web/dist in a node container (no Node needed on the host).
web-build:    ; podman run --rm -v "$$PWD/web":/web:Z -w /web docker.io/library/node:22-slim sh -c "npm ci --no-audit --no-fund && npx tsc -b --noEmit && npx vite build"
# Playwright e2e: isolated library UI tests + live-stack tests (STUDIO_URL, default :8190). Screenshots: tests/e2e/artifacts/
e2e: web-build ; uv run $(E2E_DEPS) python -m playwright install chromium && uv run $(E2E_DEPS) pytest tests/e2e -v
e2e-gpu:      ; E2E_GPU=1 uv run $(E2E_DEPS) pytest tests/e2e/test_live_flow.py -v -s
