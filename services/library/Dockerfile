# library-service (CPU): Asset Studio web UI + library API + ComfyUI proxy.
FROM docker.io/library/node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npx tsc -b --noEmit && npx vite build

FROM python:3.12-slim-bookworm
RUN pip install --no-cache-dir uv==0.12.1
WORKDIR /app
COPY services/library/requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt
COPY comfyui/custom_nodes/line_a/jobcore /app/jobcore
COPY services/library/app /app/app
COPY --from=web /web/dist /app/web
ENV PYTHONUNBUFFERED=1 SRV_ROOT=/srv WEB_DIR=/app/web
EXPOSE 8190
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8190"]
