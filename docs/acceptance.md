# Modular-system baseline (2026-09-30, head 47c20db)

Host: macOS (Apple silicon), no Docker, no GPU, empty `models/`. Web build run with the local Node 22 toolchain
(same commands as `make web-build`, which needs a container runtime).

| Suite | Command | Result |
|---|---|---|
| Lint | `make lint` | pass |
| Backend (unit + contract + regression) | `make test` | 369 pass after stabilising 3 pre-existing test defects (below); 366 pass / 3 fail at 47c20db as-is |
| Frontend type-check + build | `npm ci && npx tsc -b --noEmit && npx vite build` | pass |
| Browser e2e | `uv run --group e2e pytest tests/e2e -m e2e` | 26 pass, 3 fail (environment: see below) |

Stabilised test defects (test-only, no product change):
- `test_repo_lock_marks_dinov3_pending` expected gated-pending hashes; the lock has pinned every DINOv3 file since
  the 2026-09-29 audit, so absent weights read `missing`. Now `test_repo_lock_pins_dinov3`.
- `test_grouped_3d_builds_do_not_thrash_gpu1_and_other_items_continue`: the `:build-approved` wave creates per-item
  tasks in separate transactions; a fast lane could start the first sample before the second existed. The test now
  pauses the run around the wave (the grouping rule itself is unchanged).
- `test_gpu_acquire_failures_are_visible_then_bounded`: tasks touched within the backoff slack (10 ms) of the block
  start are, by design, treated as queued after it; on a fast machine the test's tasks were. They are now backdated.

Environment-dependent e2e (unchanged): `test_full_concept_lifecycle`, `test_3d_build_and_reexport` and
`test_sprite_build_shows_cutout_and_pivot` need model files under `models/`, because generation readiness is gated on
local weights even with the SIMULATED engine. They pass on the GPU host; on this host "Save and run" stays disabled.
Node mode (Phase 2) moves model verification to runners, which removes this coupling.

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

## Build profiles, real GPU (2026-09-30)

`tests/gpu/test_model3d_profiles.py`: **1 pass in 9 m 19 s**. It ran on the real podman stack (2x RTX 4090, worker3d
image with `geometry_policy.v1`) at commit 2099d48 plus the roughness-rounding fix below. Evidence is in
`tests/gpu/artifacts/profiles/` (report.json, previews; git-ignored). Previews are CPU renders, not a game-engine render.

| Asset (profile) | Build | Triangles | Components | alphaMode / doubleSided | Roughness median before → after | Notes |
|---|---|---|---|---|---|---|
| Mossy boulder (stone: metallic 0, roughness ≥ 0.7) | 206 s | 32 476 | 2 839 | OPAQUE / true | 0.988 → 0.988 (min 0.910) | already rough; metallic factor 0 |
| Wooden barrel (painted: metallic 0, roughness ≥ 0.75, single-sided) | 166 s | 38 904 | 140 | OPAQUE / false | 0.580 → 0.749 | clamp raised the glossy parts |
| Oak tree (foliage: preserve parts, no hole filling, alpha auto, two-sided, roughness ≥ 0.8) | 169 s | 39 742 | 463 | **MASK** / true | 0.624 → 0.800 | auto: 29.5 % texels below cutoff 0.5 |
| Oak tree, A/B rebuild of the same raw with default geometry + opaque | 18 s | 36 936 | 452 | OPAQUE / false | (profile clamp kept) | no resampling (same sample checkpoint) |

Findings:
- Auto alpha separated the assets. The tree measured 29.5 % transparent texels (15.8 % in an earlier run). The boulder
  and barrel measured 0.0 %. The 1 % threshold held on this sample of three assets, which is not a calibration.
- The exporter cleanup did not visibly delete foliage. Preserving small parts and disabling hole filling changed the
  part count by a few percent (463 vs 452; 1 087 vs 1 177 in the earlier run), and the previews are nearly identical.
  TRELLIS.2 reconstructs leaves as geometry, and MASK mainly opens small gaps at leaf edges. Decode-time hole filling
  (before the raw is stored) is still uncontrolled.
- `components` counts connected geometry islands. Rocks also come out as thousands of islands (moss and debris), so
  the advisory `single_component` check says little about generated organic assets.
- An earlier run found 8-bit rounding putting a clamped value just below the minimum (0.698 for 0.7). The bounds now
  round inward (unit test `test_roughness_bounds_round_inward`).

## Asset variants and families + design-v2 UI (2026-09-29)

CPU evidence (SIMULATED engines; fake edit engine derives its output from the source image). Commits 2a197fa..d24337a.

| Suite | Command | Engine | Result |
|---|---|---|---|
| Lint | `make lint` | — | pass |
| Backend (unit + contract + regression) | `uv run pytest` | SIMULATED | 295 pass |
| Browser e2e (all screens, variants wizard, family grouping, Jobs/Batches) | `make e2e` | SIMULATED | 28 pass |
| Real GPU acceptance (variants) | see below | real | **not run yet** |
| Docker (not Podman) deployment | — | — | **not run** |
| Egress-blocked run with the edit model | — | — | **not run** (`make acceptance-offline` was not extended/re-run for the edit model) |

Mapping to the spec's acceptance IDs (test names are in `tests/`):

| IDs | Tests |
|---|---|
| VD (drafts, capabilities, Jobs) | `contract/test_api_variants.py::test_vd01_vd02…`, `test_vd03…`, `test_vd04…`, `test_vd05…`, `test_vd06…`, `test_vd07…`, `test_vd08…`, `test_vd11…`; `test_api_variants_direct.py::test_vt05_vd10_vp01…` |
| VG (generation) | `contract/test_api_variants_generate.py::test_vg01…`–`test_vg04…`, `test_vg06…`; `test_api_rounds_refs.py::test_vg05…`; `unit/test_comfy_edit.py` (conditioning edges, upload, edit submit); VG07–VG12 (real model canary, VRAM, offline, T2I↔edit switch) need the GPU run |
| VT (transforms) | `unit/test_transforms.py::test_vt01…`–`test_vt04…`, `test_vt06…`, `test_vt12…`; `contract/test_api_variants_direct.py::test_vt05…`, `test_vt08…`, `test_vt13…` |
| VQ (variant QA) | `contract/test_api_variant_qa.py::test_vq01…`, `test_vq02…`, `test_vq02b…`, `test_vq03…`, `test_vq06…`; `unit/test_aux_v2.py::test_compare_*` |
| VP (publication) | `contract/test_api_variants_direct.py::test_vp01` (in `test_vt05_vd10_vp01…`), `test_vp02…`, `test_vp03…`, `test_vp04…` |
| VL (families/library) | no VL-named tests; covered by `contract/test_api_families.py` (grouped/filtered listing, detail family fields, family endpoints + rename, `derived_from`) |
| VR (recovery) | `regression/test_variant_recovery.py::test_vr01…`–`test_vr08…`, `test_vr06b…` |
| VS (scheduling) | `contract/test_variant_scheduling.py::test_vs01…`–`test_vs04…` |
| VM (migration) | `contract/test_variant_scheduling.py::test_vm01…`–`test_vm03…` |
| Planning / rounds / references | `contract/test_api_variant_planning.py`, `contract/test_api_rounds_refs.py`, `unit/test_aux_v2.py` |
| UI | `e2e/test_ui_variants.py`, `test_ui_job_workspace.py`, `test_ui_jobs_list.py`, `test_ui_batches.py` |

Defects found and fixed by the recovery/scheduling/migration tests: lost QA downstream planning, cancelled generation
re-run, backoff race (flaky test), orphaned engine prompt on cancel (see TODO.md Phase E).

### GPU acceptance (variants) — 2026-09-29, 2x RTX 4090, project Demo 3D

`tests/gpu/test_variants.py`: **5 passed in 5067 s** (real engines; evidence in `tests/gpu/artifacts/variants/`, git-ignored:
per-candidate PNGs, contact sheets, QA summaries, final previews, diversity reports, `evidence.json`, `passes.json`,
1 s GPU/RAM samples).

| Scenario | Result |
|---|---|
| Pine: VLM-suggested plan, 6 rows x 4 candidates (24 edits), 6 TRELLIS.2 builds, publish | pass: 6 derived assets in the pine family, derivation.source = exact pine version, pine versions unchanged |
| Crate (non-plant): 2 manual rows x 2 candidates, build, publish | pass (functional); one approved candidate was a near-copy of the source — see finding |
| Icon (2D): 2 rows x 2 candidates, cut-out + sizes, publish | pass: icon_32/64/128/256 + image published |
| Direct transform (Treasure chest): 1.2 m bottom_center + 0.5x | pass: cpu lane only, 0 ComfyUI prompts, 0 worker3d executions, preservation checks ok, 6.9 s |
| T2I after edit passes (switch edit→T2I→edit) | pass: T2I workflow unaffected; 2 gpu0 model switches total |
| `make acceptance-offline` | pass (simulated Studio: browser makes no off-host requests; NOT an egress-blocked run of the edit model) |

Numbers: edit ≈123 s/candidate (1024², 40 steps); TRELLIS.2 sample ≈74 s + bake ≈14 s per build in grouped passes;
GPU0 peak 24072/24564 MiB (98 %, no headroom); GPU1 peak 20153 MiB; host RAM peak 104 GB (ComfyUI container 31 → 52 GB
with both GPU0 models cached); ComfyUI model loads: unavailable (engine reports none). Conditioning: every candidate's
recorded conditioning sha256 = the plan's frozen primary reference, output differs from it.

Finding fixed after the run: a candidate failing `variant_change` could still be "recommended" (the check was minor), so
first-recommended selection published a source clone. `variant_change` is now a major advisory check (not recommended,
still overridable). Remaining: diversity flagged 5 of 6 pine picks as near-duplicates (advisory, selection is manual);
subtle rows (compact/narrow/tall) vary modestly. Generative variants stay **experimental** until an egress-blocked run
of the edit model (spec §8.5) is recorded.

## Hardening pass (review of 8de5fe8) — 2026-09-29

CPU evidence only (SIMULATED engines). Lint clean; `uv run pytest` 358 pass; `make e2e` 28 pass; web build ok.

| Finding | Fix | Tests |
|---|---|---|
| R01 build accepted under another approval | approve clears/restores the matching attempt; accept + publish check the run's own candidate binding (`approval_mismatch`); history labels from each run's decision | `contract/test_review_binding.py` (Job + Batch routes) |
| R08 gate commands not replay-safe | approve/accept via `commands.execute` (planned per-unit decisions, idempotent effects) | `regression/test_gate_replay.py` |
| R02 pause not persisted / wave scope | `run_controls` table enforced at create/ready/claim/retry; `require_wave` (open run, frozen selection) | `regression/test_run_control.py` |
| R03 retry second owner | transactional retry + claim guard (409 `busy`) | `regression/test_task_ownership.py` |
| R04 deferred QA stranded | savepoint (no partial chain); re-admitted after each task outcome + retry-loop tick | `regression/test_task_ownership.py` |
| R05 JPEG prepared input | profile `image_prepare.v2`: EXIF transpose, sRGB, alpha composite, PNG; old non-PNG plans fail actionably | `contract/test_variant_inputs.py` |
| R06 renderer ignores alpha/factors | renderer `cpu_lambert.v2`: texture x factor x vertex colour, MASK/BLEND cut-out, two-sided light, required-extension refusal | `unit/test_render_materials.py` (analytic oracles) |
| R09 readiness | edit workflow readiness gates generative methods; effective exporter checked | `contract/test_variant_inputs.py`, `test_review_binding.py` |
| R07 provenance from current lock | generation-time model/workflow/licence receipts; `derived_licence` never better than the source | `unit/test_provenance_receipts.py` |
| R10 worker/admission failures | worker3d `spool.Executor` survives spool/disk errors, liveness in `/health`; GPU1 acquire failures recorded, blocked after 6 | `unit/test_worker3d_spool.py`, `test_task_ownership.py` |
| R11 index rebuild race | upserts during collection re-applied at swap; grouped cursor revision read with its page | `unit/test_index_concurrency.py` |

### Release scope matrix (at this pass)

| State | Capabilities |
|---|---|
| Implemented and tested (CPU/simulated) | Jobs + Batches with run control, review gates, direct variants, family grouping/search, candidate rounds, build attempts, provenance receipts, index rebuild |
| Implemented, validated on target hardware at 8de5fe8 only | generative variants (GPU run above predates this pass; renderer v2 + input profile v2 not yet GPU-run) |
| Experimental | source-conditioned generative variants (no egress-blocked edit-model run) |
| Deferred | files + manifest exporter (folder/ZIP), project style wizard, Surface/Seamless materials, S3/GC, style-LoRA registration, Docker (non-Podman) run |

### Process-level failure injection

`make test-process` (`tests/process/`, marker `process`, not part of `make test`): Studio (`assetstudio serve`,
`STUDIO_EXECUTION=nodes`), a compute runner (`assetstudio-node run`, real HTTP engine clients, `simulated: false`) and a
fake aux engine server (`tests/process/fake_engines.py`, real `worker_common.lease` semantics, controllable delay/hold)
run as separate OS processes and are killed with SIGKILL independently. Model receipts are the one simulated part
(pre-seeded for Studio's real catalog sha; real ones need the weights). Evidence is CPU/simulated, not GPU proof.

| Scenario | Proves |
|---|---|
| A07 Studio killed mid-attempt | task `reconciling`, attempt keeps its runner and generation 1 (never re-placed), runner delivers, engine computed once |
| A08 runner killed after spooling | spooled output recovered from the runner's spool after restart, committed, spool empty, no recompute |
| A09/A10 runner killed while executing | lease expiry -> attempt and device claim `uncertain` (not freed); `:declare-lost` offers generation 2 but the device stays uncertain; restarted runner's fresh inventory releases it; generation 2 commits |
| R7 engine outlives the agent | agent process killed with a request in flight in the engine process; a new agent's barrier reports the slot `unknown`, admits nothing (late request fenced) until the request ends, then `ready` |

Results: run with `make test-process`.

### Ephemeral runners (simulated)

`tests/contract/test_ephemeral_runner.py` (WP2.12, in-process, SIMULATED engines, part of `make test`): an ephemeral
group registers a runner (`RegisterResponse.ephemeral`), exactly one attempt runs and commits, the agent heartbeats
`safe_to_terminate` only after the receipt was delivered (spool and state empty) and deregisters; the runner is then
`revoked`, counts in neither `runner_readiness` nor `gpus`, and no second attempt is offered (a forced second accept
returns 409 `admission_rejected`); deregistering with custody not transferred returns 409. Its device claim stays
`free` (not retired); scheduling is prevented by the revoked runner.

### Node mode — real GPU acceptance (tests/gpu_nodes)

`make acceptance-gpu-nodes` (marker `gpu_nodes`, not part of `make test`). Operator-run on the 2 x 4090 host against a
stack started with `make up-nodes` (Studio without `/models`, runner with the GPUs). Skips unless `STUDIO_URL` is set
and `/api/v1/runtime` reports `engine_mode == "nodes"`; fails fast with `runner_readiness` reasons if a needed
operation is not ready. Strict like `tests/gpu`: a failed stage is a failure. The direct-mode suite cannot certify node
topology (it inspects ComfyUI directly and assumes fixed gpu0/gpu1 lanes), hence this separate suite.

| Scenario | Spec | Env flag | Proves |
|---|---|---|---|
| Studio has no models | A01 | `GPU_NODES_DOCKER=1` for the container check | every ready model is runner-verified, GPUs come from runners; `/models` absent in the Studio container |
| 2D lifecycle (concept_art, 2 items) | A02 | - | 8 candidates, 8 `image.t2i` attempts all committed on generation 1, QA, 2 publications, spool drained |
| 3D lifecycle (model3d, 1 item) | A02 | - | generate/export attempts committed on the aux3d slot, valid GLB, published |
| Runner killed mid-sample | A08 | `GPU_NODES_CHAOS=1` | `docker compose kill/start runner`; one TRELLIS attempt, generation 1, single publication |
| Studio restarted mid-generation | A07 | `GPU_NODES_CHAOS=1` | `docker compose restart studio`; exactly 8 executions, no generation 2 |
| Two runners | A02, A04 | `GPU_NODES_RUNNER_B=1` | distinct device UUIDs even at index 0; an operation only one runner can serve lands on it |

`NODES_COMPOSE` overrides the compose flags (default `docker compose -f compose.yml -f compose.nodes.yml`).
Results: **not yet run on hardware**.

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
- Variants: see TODO.md ("Known limitations / follow-ups"). Edit-model VRAM headroom is ~0.5 GB on a 24 GB card.
