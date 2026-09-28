COMPOSE ?= podman-compose
.PHONY: build up down logs ps models verify smoke test lock-comfyui

build:        ; $(COMPOSE) build
up:           ; $(COMPOSE) up -d
down:         ; $(COMPOSE) down
logs:         ; $(COMPOSE) logs -f --tail=200
ps:           ; $(COMPOSE) ps
models:       ; ./scripts/download-models.sh
verify:       ; uv run --script scripts/verify-models.py
smoke:        ; ./scripts/smoke-test.sh
test:         ; uv run --with pytest --with pyyaml --with pydantic pytest tests/unit -q
# Freeze ComfyUI's transitive deps from the built image into comfyui/constraints.txt.
lock-comfyui:
	podman run --rm localhost/line-a-comfyui:dev pip freeze --exclude-editable \
	  | grep -v -E '^(torch|torchvision|torchaudio|nvidia-|triton)' > comfyui/constraints.txt
