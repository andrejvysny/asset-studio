# Acceptance evidence — Phases 0–2 (2026-09-28)

Host: 2 × RTX 4090 24 GB, Linux, Podman 4.9.3 + podman-compose 1.0.6 (`docker` on this host is a podman wrapper).
Branch `refactor/assetstudio-core` (uncommitted working tree, based on f5ca95c).

## Results by evidence type

| Suite | Command | Engine | Result |
|---|---|---|---|
| Lint | `make lint` | — | pass |
| Unit (core, storage, runtime contracts, mesh) | `uv run pytest tests/unit` | — | 42 pass |
| Contract (API, lifecycle, recovery) | `uv run pytest tests/contract` | SIMULATED | 18 pass |
| Frontend type-check + build | `make web-build` (node container) | — | pass |
| Browser e2e (all 10 project screens + full lifecycle) | `make e2e` | SIMULATED | 13 pass |
| Offline (no off-host browser requests; decoders bundled) | `make acceptance-offline` | SIMULATED | pass |
| **Real stack, strict** | `make PODMAN=1 up` + `GPU_QUALITY=1 make acceptance-gpu` | ComfyUI + aux, real models | **2 pass (8 m 49 s)** |
| Real 3D candidate QA (masks + VLM), ad hoc script | see below | real | pass (10/10 coverage) |
| Real BiRefNet `/cutout` + acknowledged `/unload` | ad hoc | real | pass (1.7 s, 1024² mask) |
| Docker (not Podman) deployment | — | — | **not run** (Docker not installed) |
| 3D build (TRELLIS.2) | — | — | **not run**: blocked, DINOv3 access pending |

## Real-stack run (tests/gpu/test_acceptance.py)

`test_seven_item_batch_real_stack` — concept recipe, Lightning 8-step speed preset, 1024²:
- real Qwen3-VL enhancement for 7 items (no simulated flags), one prompt edited before confirmation;
- confirm-and-generate for 7 items; **Studio container restarted mid-generation** (`podman restart`); the operation
  was reconciled (`reconciled_after_restart: true`) and completed;
- exactly **28 candidates, 28 distinct seeds, 28 ComfyUI executions** (no duplicate submissions after restart);
- real VLM QA answers (pass/fail, strictly parsed);
- 4 approved (recommended or explicit override), 1 marked for regeneration, 2 left undecided → build ran for
  exactly the 4 approved items; regeneration produced a new prompt revision + candidate set for that row only;
- 4 valid builds accepted and published; library shows 4 assets; `assetstudio storage verify` re-hashed every
  published artifact against its blob (0 problems); version licence status `review` (ComfyUI GPL + Lightning LoRA
  review), never `cleared` by default.

`test_quality_preset_single_item` — default 50-step path, 1 item × 4 candidates generated with `steps == 50`.

Journal timings for the run (operation wall time incl. queueing): enhance 2 ops 28 s · generate 3 ops 478 s ·
QA 9 ops 39 s · build 1 op 1 s · publish 1 op < 1 s. VLM loaded once for the whole run (aux `loads.vlm == 1`).

Ad hoc 3D-candidate check: 1-item `model3d` batch → 4 real candidates → QA with BiRefNet masks + VLM, coverage
10/10 for every candidate (`no_shadow` failed on 3 as a minor check → still `recommended`); build correctly refused:
"3D engine pending: native TRELLIS.2 worker + DINOv3 access (Phase 3)".

## Known limitations (this release)

- 3D builds, re-export and mesh processing: Phase 3 (needs DINOv3 approval; no fallback model by decision).
- Icon / sprite / material / sheet / VFX builds: Phase 5 (candidate generation + review work for image kinds).
- S3, GC, retention expiry, export targets: Phase 4 (UI controls disabled with reasons).
- Style LoRA registration UI/CLI not implemented (none bundled; selecting one blocks dispatch with a clear error).
- Pipelines parameter editing is via Schema → YAML (typed validation); inline editor pending.
- Response types in the web client are hand-written (no OpenAPI response models yet).
- Model-load counts: the scheduler records passes; ComfyUI does not report its own loads.
