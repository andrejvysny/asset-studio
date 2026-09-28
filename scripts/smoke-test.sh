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
STUDIO=${STUDIO_PORT:-8190}
curl -sf "http://127.0.0.1:$STUDIO/api/catalog" >/dev/null && ok "Asset Studio API reachable" || bad "Asset Studio not reachable on :$STUDIO"
curl -sf "http://127.0.0.1:$STUDIO/comfy/system_stats" >/dev/null && ok "Studio -> ComfyUI proxy" || bad "Studio proxy to ComfyUI failing"
for svc in prompt-service:8001 trellis-worker:8002; do
  name=${svc%%:*}; port=${svc##*:}
  # workers are on an internal network; ask the Studio (same network) for their health, not just HTTP 200
  st=$(curl -sf "http://127.0.0.1:$STUDIO/api/runtime" | python3 -c "import sys,json;w=json.load(sys.stdin)['workers']['$name'];print('ok' if w.get('reachable') and w.get('ok', True) else 'reachable-not-ready' if w.get('reachable') else 'down', json.dumps(w.get('models','')))" 2>/dev/null)
  case "$st" in ok*) ok "$name ready";; reachable*) bad "$name reachable but not ready: ${st#* }";; *) bad "$name down";; esac
done
exit $FAIL
