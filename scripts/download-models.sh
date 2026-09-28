#!/usr/bin/env bash
# Download pinned models (config/models.yaml). Needs network; never runs at container start.
# Usage: ./scripts/download-models.sh [--with-optional] [--only name ...]
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a
command -v uv >/dev/null || { echo "uv required: https://docs.astral.sh/uv/" >&2; exit 1; }
uv run --script scripts/download_models.py "$@"
uv run --script scripts/verify-models.py
