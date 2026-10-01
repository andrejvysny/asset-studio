# studio (CPU): AssetStudio web UI + Studio API + coordinator. No CUDA, no models loaded here.
FROM docker.io/library/node:22.20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
COPY web/scripts/ scripts/
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npx tsc -b --noEmit && npx vite build

FROM docker.io/library/python:3.12.11-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY packages/ packages/
COPY services/studio/ services/studio/
RUN uv sync --frozen --no-dev
COPY config/ /app/config/
COPY contracts/ /app/contracts/
COPY comfyui/workflows/ /app/comfyui/workflows/
COPY --from=web /web/dist /app/web
# 1777: the runtime uid differs under rootless Podman keep-id; the dir is private to this container anyway.
RUN useradd --uid 1000 --create-home studio && mkdir -p /data/instance && chmod 1777 /data/instance
USER studio
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 STUDIO_HOST=0.0.0.0 STUDIO_PORT=8190 STUDIO_MCP_HOST=0.0.0.0 STUDIO_MCP_PORT=8191 \
    STUDIO_WEB_DIR=/app/web STUDIO_CONFIG_DIR=/app/config STUDIO_WORKFLOWS_DIR=/app/comfyui/workflows \
    STUDIO_INSTANCE_DIR=/data/instance STUDIO_PROJECT_ROOTS=/projects STUDIO_MODELS_ROOT=/models \
    STUDIO_INTEGRATION_HOST=0.0.0.0 STUDIO_INTEGRATION_PORT=8192 STUDIO_INTEGRATION_CONTAINER_BIND=1 \
    STUDIO_CONTRACTS_DIR=/app/contracts/godot-integration/v1
EXPOSE 8190 8191 8192
CMD ["assetstudio", "serve"]
