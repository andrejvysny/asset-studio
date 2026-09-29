# AssetStudio

Self-hosted workbench to plan, generate, import, review, version and organise digital assets
(3D models, sprites, icons, VFX flipbooks, materials, sprite sheets, concept art). Projects are portable folders;
inference runs only on your machine (ComfyUI + local models). Nothing about a particular game is built in.

- **Studio** (`services/studio`, :8190): UI + product API + scheduler. Owns all state and every human gate.
- **ComfyUI** (GPU0, :8188 localhost): image engine behind a versioned adapter; its UI stays available as an advanced tool.
- **aux** (GPU1): text enhancement, advisory visual QA (Qwen3-VL), segmentation (BiRefNet). Stateless.

## Quick start (Linux, 2 × 24 GB NVIDIA GPUs)

```sh
cp .env.example .env            # set HF_TOKEN; paths; GPU ids
make models                     # explicit download from config/models.lock.yaml (never at runtime)
make verify                     # dependency closure check (`make verify-full` = sha256)
make build up                   # Docker. Podman: `make PODMAN=1 build up`
open http://127.0.0.1:8190      # create an empty project, define categories in Schema
```

Library-only use (no GPUs/models): `STUDIO_ENGINE=none`. Demo/test mode with a clearly labelled
**simulated** engine: `STUDIO_ENGINE=fake`.

## Status of this release (Phases 0–2)

| Capability | State |
|---|---|
| Projects, schema inheritance, snapshots, shot list, import (PNG/JPEG/GLB), versions, publication | working |
| Batches: enhance → confirm → candidates + advisory QA → approve / regenerate → build → accept → publish | working |
| Concept art build (approved original → final) | working |
| 3D build (TRELLIS.2) | blocked: DINOv3 access pending; native worker is Phase 3 |
| Icon / sprite / material / sheet / VFX builds | Phase 5 (generation of candidates works for image kinds) |
| S3 storage, GC, export targets (files / Godot / Git) | Phase 4 |

See `TODO.md` for the tracked checklist, `docs/` for architecture, installation and acceptance evidence.

## Development

```sh
uv sync                         # Python 3.12 workspace (packages/ + services/studio)
make lint test                  # ruff + unit/contract tests (simulated engine)
make web-build e2e              # frontend type-check/build in a Node container + Playwright
make acceptance-gpu             # strict real-stack run (needs `make up`)
```
