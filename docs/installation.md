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

## Single machine with a compute runner (node mode)

Studio runs without model weights; a `runner` container owns the GPUs and engines (profile S,
`docs/modular/compute-runner.md`). Direct mode stays the default.

Prerequisites: Docker Compose >= 2.24 (`!override`/`!reset` tags; `make PODMAN=1` is refused) and the NVIDIA Container
Toolkit. The models from `make models` must already be on this host.

1. `make build`
2. `make runner-token` — creates the `local` runner group and writes its registration token to
   `secrets/runner_registration_token` (0600, gitignored).
3. `make up-nodes` — `docker compose -f compose.yml -f compose.nodes.yml up -d`.
4. Open **Runtime → Compute runners**: `gpu-box` should be fresh with two slots (`image`, `aux3d`) and verified models.
5. Activate node execution: `make switch-nodes` (`assetstudio execution switch --to nodes`). This command arrives with
   WP2.5b; until then the target fails with an argparse error and Studio stays on the compose-set mode.

Model verification: on first start the runner hashes every file of the catalog Studio sends (full sha256), then caches
the result per catalog; later starts only re-check file identity.

Changes vs direct mode: Studio has no `/models` mount and no engine URLs; ComfyUI has no host port (only the runner
submits prompts); the runner alone sees the GPUs (`utility` capability for UUID discovery; the engines keep compute).
GPU ids are taken from `GPU_IMAGE_ID`/`GPU_AUX_ID` and appear in the runner as `index:0`/`index:1` in `config/runner.single.yaml`.

Rollback: `make down-nodes && make up`, only while no node work is in flight. After WP2.5b use
`assetstudio execution switch --to direct` first.

Troubleshooting:
- Slot `unknown`: the recovery barrier could not confirm the workers idle. `docker compose restart aux worker3d`, then wait.
- `forbidden_scope` on inventory: the GPU UUID is already claimed by another runner (one runner per host; check for a
  stale runner from another machine or name).

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
