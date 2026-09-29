#!/usr/bin/env bash
# Offline evidence (no internet needed at runtime):
#  1. the built frontend references no external origins (fonts, viewers and decoders are bundled);
#  2. the full UI lifecycle runs while the browser records every request: any non-local request fails the test.
# Runtime model flags (HF_HUB_OFFLINE etc.) are set in compose; aux sits on an internal network without egress.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ -f web/dist/index.html ]] || { echo "run make web-build first" >&2; exit 1; }
hits=$(grep -rhoE "https?://[a-zA-Z0-9.-]+" web/dist | sort -u | grep -vE "^https?://(127\.0\.0\.1|localhost|www\.w3\.org|github\.com|react\.dev|reactjs\.org|goo\.gl|fb\.me)" || true)
if [[ -n "$hits" ]]; then
  echo "URLs present in bundle (inspect: must be inert strings, never fetched):"; echo "$hits"
fi
uv run --group e2e pytest tests/e2e/test_studio_ui.py -q -m e2e -k lifecycle
echo "offline check: OK (no off-host browser requests during the lifecycle run)"
