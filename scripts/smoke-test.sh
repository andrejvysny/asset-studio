#!/usr/bin/env bash
# Smoke test: containers up, endpoints reachable, models present, Line A nodes loaded.
set -uo pipefail
cd "$(dirname "$0")/.."
[[ -f .env ]] && set -a && source .env && set +a
PORT=${COMFYUI_PORT:-8188}; FAIL=0
ok(){ echo "  OK   $*"; }; bad(){ echo "  FAIL $*"; FAIL=1; }
RT=$(command -v podman || command -v docker)

uv run --script scripts/verify-models.py >/dev/null && ok "models verified" || bad "models (run scripts/verify-models.py)"
curl -sf "http://127.0.0.1:$PORT/system_stats" >/dev/null && ok "ComfyUI reachable" || bad "ComfyUI not reachable on :$PORT"
curl -sf "http://127.0.0.1:$PORT/object_info/LineACreateJob" | grep -q LineACreateJob && ok "line_a nodes loaded" || bad "line_a nodes missing"
for svc in prompt-service:8001 trellis-worker:8002; do
  name=${svc%%:*}; port=${svc##*:}
  # workers are on an internal network; probe from inside the comfyui container
  $RT exec line-a_comfyui_1 python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://$name:$port/health',timeout=5).status==200 else 1)" \
    2>/dev/null && ok "$name healthy" || bad "$name not healthy"
done
exit $FAIL
