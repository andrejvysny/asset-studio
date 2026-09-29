# Installation and operation

## Host requirements (default generation profile)

- Linux, NVIDIA driver + NVIDIA Container Toolkit, 2 × 24 GB GPUs (GPU0 image engine, GPU1 aux). VRAM is not pooled.
- Docker Compose v2 (baseline) or Podman 4.9 + podman-compose 1.0.x (override file).
- Disk: model closure ≈ 62 GB today (`make doctor` prints the remaining download size from the lock) + project data.
- Library-only mode needs neither GPUs nor models.

## Steps

1. `cp .env.example .env` and set `HF_TOKEN`, `PROJECTS_DIR`, `MODELS_DIR`, GPU ids.
2. `make models` — downloads exactly the files pinned in `config/models.lock.yaml` (repo + revision + file list).
   Gated repos (DINOv3) report `PENDING` until your Hugging Face access request is approved; dependent recipes stay
   disabled with the exact missing files shown on the Runtime screen.
3. `make verify` (sizes) / `make verify-full` (sha256, cached per file identity). README-only or partial downloads fail.
4. `make build up` (Docker) or `make PODMAN=1 build up`.
5. Open `http://127.0.0.1:8190`, create a project (it starts empty), add categories under **Schema**.

## Operations

| Command | Purpose |
|---|---|
| `uv run assetstudio doctor` | GPUs, disk, model closure, project roots |
| `uv run assetstudio models verify [--full]` | dependency closure |
| `uv run assetstudio project create/list/register` | project roots (server paths under `STUDIO_PROJECT_ROOTS`) |
| `uv run assetstudio storage verify <project>` | every published artifact re-hashed against its blob |
| `uv run assetstudio storage reindex <project>` | rebuild the search index from manifests |
| `make logs` / `make down` | service logs / stop |

Inside containers use `docker compose exec studio assetstudio …` (Podman: `podman exec assetstudio_studio_1 …`).

## Notes

- The ComfyUI UI at `http://127.0.0.1:8188` is an advanced tool. Running your own heavy graphs there competes with the
  Studio scheduler for GPU0; pause production work first.
- 3D: `worker3d` shares GPU1 with `aux` under Studio ownership (explicit unload acks). DINOv3 is gated: accept the
  licence on Hugging Face, put `HF_TOKEN` in `.env`, then `make models` + `make verify-full`. Set `WORKER3D_URL=` (empty)
  to run without 3D. The research exporter (NVIDIA nvdiffrast, evaluation only) needs a separate image:
  `RESEARCH_EXPORTER=1 RESEARCH_EXPORTER_TAG=research make PODMAN=1 build` and the same variables for `up`.
- Moving a project: stop the Studio, copy the project root, `project register` on the new host. Model weights are not
  part of a project.
