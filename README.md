# AssetStudio

Self-hosted workbench to plan, generate, import, review, version and organise digital assets
(3D models, sprites, icons, VFX flipbooks, materials, sprite sheets, concept art). Projects are portable folders;
inference runs only on your machine (ComfyUI + local models). Nothing about a particular game is built in.

- **Studio** (`services/studio`, :8190): UI + product API + scheduler. Owns all state and every human gate.
- **ComfyUI** (GPU0, :8188 localhost): image engine behind a versioned adapter; its UI stays available as an advanced tool.
- **aux** (GPU1): text enhancement, advisory visual QA (Qwen3-VL), segmentation (BiRefNet). Stateless.
- **MCP** (:8191, in the Studio process): remote AI agents drive everything the UI can do, bearer-token auth
  (`assetstudio mcp create <name>`). See `docs/mcp.md`.

## Quick start: Studio on any host (no GPU)

```sh
cp .env.example .env            # optional: ports, PROJECTS_DIR
docker compose up -d --build    # Docker Compose >= 2.24.4; = `make build up`
open http://127.0.0.1:8190      # create an empty project, define categories in Schema
```

Studio (UI + API) is CPU-only and listens on loopback without operator login. Generation needs GPU **runners** on
other machines (`compose.node-remote.yml`, `docs/installation.md`); without runners Studio is a library/import/review
tool. TLS, proxying and auth for real deployments live outside this repository.

## Quick start: all-in-one GPU box (Linux, 2 × 24 GB NVIDIA GPUs)

```sh
cp .env.example .env            # set HF_TOKEN; paths; GPU ids
make models                     # explicit download from config/models.lock.yaml (never at runtime)
make verify                     # dependency closure check (`make verify-full` = sha256)
make gpu-build gpu-up           # compose.gpu-local.yml. Podman (legacy): `make PODMAN=1 gpu-build gpu-up`
open http://127.0.0.1:8190
```

In this stack `STUDIO_ENGINE=none` gives library-only use; `STUDIO_ENGINE=fake` a clearly labelled **simulated** engine.

## Status of this release (Phases 0–2 + 5)

| Capability | State |
|---|---|
| Projects, schema inheritance, snapshots, shot list, import (PNG/JPEG/GLB, frame sequences, material bundles), versions, publication | working |
| Asset families; variants: direct size transforms (GLB scale, raster resize/pad) and **experimental** generative variants (Qwen-Image-Edit-2511 source-conditioned edits; 3D re-built via TRELLIS.2), rounds, references, advisory variant QA + diversity report | working (generative: experimental until GPU acceptance passes) |
| Jobs (one asset each) and Batches (groups of Jobs), stage-first scheduling | working |
| Production Jobs: enhance → confirm → candidates + advisory QA → approve / regenerate → build → accept → publish | working |
| Concept art build (approved original → final) | working |
| 3D build: BiRefNet → TRELLIS.2 + DINOv3 → GLB (clean exporter, no NVIDIA NC code), re-export from raw | working |
| Sprite (cut-out, canvas, pivot), icon (sized variants), material (base colour + seam gate) builds | working |
| Sprite sheet / VFX flipbook | atlas from imported frame sequences; no generation (no verified local temporal model) |
| S3 storage, GC, export targets (files / Godot / Git) | Phase 4 |

See `TODO.md` for the tracked checklist, `docs/` for architecture, installation and acceptance evidence.

## Creating variants

A variant is a new asset made from one exact published version and grouped into the source's family.

- **UI**: Assets → open an asset → *New variant* / *Create variants* wizard (method, intent, preserve/change, rows,
  references, family) → Create. This makes one one-item Job per row plus a draft Batch sharing one immutable plan;
  run them from Jobs/Batches (human gates as usual). Assets can be grouped by family.
- **API**: `GET …/assets/{id}/versions/{ver}/variant-capabilities` → `POST …/variant-drafts` → `PATCH …/variant-drafts/{id}`
  (optionally `:prepare-references`, `:analyze-source`, `:suggest-plan`, `:apply-suggestion`) → `POST …/variant-drafts/{id}:create-jobs`.
  `POST …/variant-plans/{plan}:compare-selection` starts the diversity report.
- Direct size transforms need no models. Generative image edits need the edit model (optional download, ~20 GB;
  reuses the `qwen_image_2512` text encoder + VAE; `--only` downloads just the listed keys, so include `qwen_image_2512`
  if it is not present yet):

```sh
make models ARGS="--only qwen_image_2512 qwen_image_edit_2511"   # only qwen_image_edit_2511 if 2512 is present
make verify
```

## Development

```sh
uv sync                         # Python 3.12 workspace (packages/ + services/studio)
make lint test                  # ruff + unit/contract tests (simulated engine)
make web-build e2e              # frontend type-check/build in a Node container + Playwright
make acceptance-gpu             # strict real-stack run (needs `make up`)
```
