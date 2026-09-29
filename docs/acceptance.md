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

## Phase 5 — image kinds (2026-09-29)

| Suite | Command | Engine | Result |
|---|---|---|---|
| Unit: raster (cut-out, trim, pivot canvas, variants, seam ratio), atlas (natural order, grid/pow2, caps), hostile zips (traversal, absolute, backslash, symlink, entry/expansion caps) | `uv run pytest tests/unit/test_raster_atlas.py` | — | 12 pass |
| Contract: sprite (QA-mask reuse) / icon (build-time segmentation, exact sizes) / material (original kept, seam + square checks, no invented maps) / `int_list` validation | `uv run pytest tests/contract/test_api_image_kinds.py` | SIMULATED | 4 pass |
| Contract: frame zip → VFX atlas, multi-PNG → sheet, bad params, size mismatch, unsafe zip, material bundle explicit roles (unmapped/duplicate/size mismatch rejected) | `uv run pytest tests/contract/test_api_imports_sets.py` | — | 5 pass |
| Browser: sprite build view (pivot marker, checks), frame-sequence import → flipbook player, material mapping gate → tiled preview; no off-host requests | `make e2e` | SIMULATED | 3 pass (suite total 16) |
| Full unit + contract suite | `uv run pytest` | mixed | 81 pass |
| Real stack: sprite + icon + material | `uv run pytest tests/gpu/test_image_kinds.py -m gpu` | real | **1 pass (80 s)** |

Real-stack run (`test_sprite_icon_material_real_stack`, Lightning 8-step, 1024², 2 candidates/item, real enhancement + VLM QA):
- **sprite** (brass lantern): valid, 512² RGBA canvas, bottom-centre pivot, 29.1 % opaque; mask **reused from QA** (same
  approved artifact lineage) → no second segmentation; published.
- **icon** (healing potion): valid, exact 256/128/64/32 variants, 39.9 % opaque; QA had no mask rules → real BiRefNet
  segmentation at build time on GPU1 (`simulated: false`); published.
- **material** (mossy cobblestone): build ran; seam gate **refused** it (wrap ΔE 18.5 vs neighbour ΔE 4.4 → ratio ≈ 4.0,
  limit 2.0 — visible seams when tiled). Not published. Expected behaviour of the gate, but it means Qwen-Image output is
  usually not tileable as-is (see limitations).
- `assetstudio storage verify <project>`: 0 problems.

## Phase 3 — 3D (2026-09-29)

| Suite | Command | Engine | Result |
|---|---|---|---|
| Model closure incl. DINOv3 (gated access granted, locked revision) | `make verify-full` | — | 7/7 sha256 verified |
| Licence audit at pinned refs | manual (LICENSE files) | — | nvdiffrast v0.4.0 + nvdiffrec: research/evaluation only; TRELLIS.2/o-voxel, CuMesh, FlexGEMM: MIT |
| Clean image contains no nvdiffrast (stub raises on use); rasteriser full-square coverage, Σbary = 1 | in-container smoke test | real GPU1 | pass |
| Contract: 3D build (raw/cutout/model/preview/meta, budget requested→effective→actual, QA-mask reuse, licence components), re-export reuses raw without resampling, blocked without worker, intermediates not published | `uv run pytest tests/contract/test_api_model3d.py` | SIMULATED worker | 3 pass |
| Browser: 3D build view (model-viewer, advisory budget), re-export dialog → new valid build | `make e2e` | SIMULATED | pass (suite 16/16) |
| Clean vs research rasteriser on the same real mesh + UV unwrap (2048², ~99.5k faces) | ad hoc in research image | real | coverage 51.295 % vs 51.293 % · same triangle 99.45 % · position p99 Δ 6e-8, 14 texels > 1e-3, 0 > 1e-2 · 0.14 s vs 0.006 s |
| **Real stack: 3D batch through the Studio** | `uv run pytest tests/gpu/test_model3d.py -m gpu` | real | **1 pass (4 m 17 s)** |

Real run (`test_model3d_real_stack`, wooden supply crate, category budget 2 000–40 000 triangles): real enhancement →
2 Lightning candidates → QA with BiRefNet masks + VLM → approve → build 211 s end-to-end (TRELLIS.2 `1024_cascade`
sampling 154 s incl. first load, 20.4 M raw faces, peak VRAM 11.3 GB; clean export 15 s) → **valid**: self-contained GLB,
UVs, base-colour + metallic/roughness textures, 37 712 triangles (within budget), 1 geometric component; QA mask reused;
GPU1 handed aux → worker3d with acknowledged unload → re-export at 1024² texture in 18 s reusing the same raw → accept →
publish (licence `review`: DINOv3 licence + Lightning LoRA; `exporter_clean` cleared; no nvdiffrast) →
`storage verify` 0 problems.

## Known limitations (this release)

- 3D: upstream CuMesh simplification is not deterministic (same raw → e.g. 99 808 vs 99 221 faces); upstream export
  always fills holes < 0.03 perimeter. Raw intermediates are ~100–300 MB each (retention `raw`; GC is Phase 4).
- 3D: research exporter validated directly in its image, not through a Studio run (the deployed worker is the clean image).
- Sprite sheet / VFX: no generation (no verified local temporal model); built only from imported frame sequences.
- Material builds publish base colour only; derived PBR maps are not generated (import a bundle for full map sets).
- Real generated materials usually fail the seam gate (observed ratio ≈ 4 vs limit 2): no tiling step exists yet, so a
  generated material rarely publishes without raising `seam_max_ratio` per project.
- S3, GC, retention expiry, export targets: Phase 4 (UI controls disabled with reasons).
- Style LoRA registration UI/CLI not implemented (none bundled; selecting one blocks dispatch with a clear error).
- Pipelines parameter editing is via Schema → YAML (typed validation); inline editor pending.
- Response types in the web client are hand-written (no OpenAPI response models yet).
- Model-load counts: the scheduler records passes; ComfyUI does not report its own loads.
