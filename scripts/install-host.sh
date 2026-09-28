#!/usr/bin/env bash
# Host validation (does not install anything). Prints what is missing and how to fix it.
set -uo pipefail
ok(){ echo "  OK   $*"; }; bad(){ echo "  FAIL $*"; FAIL=1; }; FAIL=0

echo "Line A host check"
command -v nvidia-smi >/dev/null && ok "nvidia driver: $(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)" \
  || bad "nvidia-smi missing -> install NVIDIA driver"
n=$(nvidia-smi -L 2>/dev/null | wc -l); [[ $n -ge 2 ]] && ok "$n GPUs" || bad "need 2 GPUs, found $n"

if command -v podman >/dev/null; then ok "podman $(podman --version | awk '{print $3}')"; RT=podman
elif command -v docker >/dev/null; then ok "docker $(docker --version)"; RT=docker
else bad "no podman/docker -> install Docker Engine or podman"; RT=; fi
{ command -v podman-compose >/dev/null || docker compose version >/dev/null 2>&1; } \
  && ok "compose available" || bad "no compose -> install podman-compose or docker compose plugin"

command -v nvidia-ctk >/dev/null && ok "nvidia-ctk present" || bad "NVIDIA Container Toolkit missing"
if nvidia-ctk cdi list 2>/dev/null | grep -q 'nvidia.com/gpu=1'; then ok "CDI spec lists nvidia.com/gpu=0,1"
else bad "CDI spec missing -> sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"; fi

if [[ -n "$RT" ]]; then
  $RT run --rm --device nvidia.com/gpu=1 docker.io/library/ubuntu:22.04 nvidia-smi -L >/dev/null 2>&1 \
    && ok "container GPU access (CDI)" || bad "container cannot see GPU1 via CDI"
fi
command -v uv >/dev/null && ok "uv present" || bad "uv missing -> curl -LsSf https://astral.sh/uv/install.sh | sh"
avail=$(df -BG --output=avail . | tail -1 | tr -dc 0-9); [[ $avail -ge 150 ]] && ok "${avail}G free disk" || bad "need >=150G free, have ${avail}G"
exit $FAIL
