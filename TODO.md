# TODO — Jobs, Batches, delivery, project style (milestone 2026-09-29)

Spec: owner handoff "AssetStudio — Jobs, Batch Execution, Library Delivery and Project Style" (baseline 60ff832).
Plan: ~/.claude/plans/act-as-senior-software-serialized-spindle.md. Traceability: docs/traceability.md.
Owner decisions: commit per green phase on master; task state journal-only; worker3d spool only; STOP after Phase 2.

## Phase 0 — baseline + fixtures
- [x] baseline 60ff832: lint clean, 83 tests pass, web-build ok (podman host)
- [x] fixture projects from a70232b + 60ff832 (tests/fixtures/projects, scripts/make_fixture_project.py)
- [x] regression tests (tests/regression, tests/worker3d) written with the fixes
- [x] `project backup` / `restore-verify`

## Phase 1 — safe state, integrity, checkpoints, compat
- [x] ids (job/bch/brn/stk/pas/att/...), domain v2 (Job/JobItem/Batch/BatchRun, v1 compat readers)
- [x] verified blobs (dedup reuse, reads, containment) H07 RI13 RI14
- [x] journal: scoped commands H06, conditional requeue, cancel intent kept H04, lane epochs
- [ ] journal StageTasks/attempts/passes/reservations H05 → Phase 2
- [x] publication: role contracts, authoritative names, receipts IM01 IM02 RI16
- [x] imports replay-safe + upload ids + decoded budgets H08 H17 RI15 IM04 IM05
- [x] BuildRun checkpoints + attach-at-create + isolated preview H01 H15 RI07–RI09
- [x] model3d target precedence H12 RI18, preflight H18, recipe compat H03
- [x] worker3d: lease/session/epoch, activity drain, executions + spool H02 RI10–RI12
- [x] raw_npz CPU validation H13 IM06; rasterizer winner + tiling H14 IM07 (make test-worker3d)
- [x] aux: exec id, lease, drain
- [x] bounded auto-retry H16, lane survives bugs RI17
- [ ] full failure taxonomy per StageTask → Phase 2

## Phase 2 — Jobs + Batch execution
- [ ] Job services (save-only create), Batch groups, run planner, runs + waves
- [ ] StageTasks per stage, planner/passes/runner, coalescing, downstream reconciliation
- [ ] v2 routers + v1 adapters, SSE ids
- [ ] web: Jobs rename, Batches screens, nav, redirects, Runtime passes
- [ ] tests JB01–JB16, RI04, IM10, e2e multi-Job
- [ ] >>> STOP for owner review <<<

## Phase 3 — library delivery + comparison (after review)
## Phase 4 — project style + prompt workflow
## Phase 5 — materials (surface/seamless, edge blend)
## Phase 6 — integrated acceptance, compose, docs

## Previous milestone (archived)
> # TODO — AssetStudio refactor
>
> Spec: SPEC.md (AssetStudio general spec, 2026-09-28). Plan: ~/.claude/plans/act-as-senior-software-iridescent-zephyr.md
> Branch: refactor/assetstudio-core. This run: Phases 0–2 (vertical slice). Decisions: legacy data discarded; Docker
> baseline + Podman override; 3D = TRELLIS.2 + DINOv3 only (no fallback; DINOv3 access pending, not downloaded).
>
> ## Phase 0 — baseline
> - [x] baseline f5ca95c: 82 unit tests pass; host docker = podman wrapper; 2x4090; 1 legacy job (discarded)
> - [x] design import (Asset Studio v2.dc.html) reviewed; support.js = dc runtime only
>
> ## Phase 1 — core, storage, import, publish
> - [x] packages/assetstudio_core: ids, kinds/origin, config + inherit/value/disabled, snapshots, QA policy, recipes, naming, seeds, lifecycle, review binding, shot-list parsers
> - [x] packages/assetstudio_storage: Repository, LocalBackend (atomic, link-CAS, blobs), ProjectStore, publication (idempotent, derived ids), SQLite index (shadow rebuild), writer lock
> - [x] packages/assetstudio_processing: image inspect/thumbnail, mask/palette metrics, GLB container + mesh validation
> - [x] models.lock.yaml + closure verifier (F07 fixed: DINOv3 README-only -> pending_access)
> - [x] Studio API: projects, config (visual/yaml, revision), assets/versions/set-current, imports, artifacts, storage view/test/reindex, runtime, capabilities
> - [x] CLI: doctor, models verify [--full], project create/list/register, storage reindex/verify
> - [x] tests: unit (core/storage/runtime/mesh) + contract (library)
>
> ## Phase 2 — batches + real image production
> - [x] shot list CRUD + CSV/YAML/MD two-phase import, derived status, claim checks
> - [x] batches/items, prompt revisions, confirm gate, regenerate, approve (exact binding), preview-best, accept, publish
> - [x] journal (held→queued, idempotency, reconcile on restart), coordinator lanes, GPU1 ownership w/ ack
> - [x] ComfyUI adapter (stock nodes, bindings, deterministic prompt ids, targeted cancel); aux service rewrite (enhance/qa/cutout/unload ack)
> - [x] concept passthrough build; 3D build blocked with reason
> - [x] tests: contract batches + recovery (simulated engine)
> - [x] web/: v2 design screens wired to API (12 screens + asset detail + new batch + 5-tab batch workspace)
> - [x] e2e Playwright vs real Studio process (simulated engine): 13 pass; offline check (decoders bundled)
> - [x] GPU acceptance (real 7-item concept batch, restart mid-pass, 28 exact executions, publish, hash verify): pass
> - [x] docs: README, docs/architecture.md, docs/installation.md, docs/acceptance.md; SPEC.md (condensed + decisions)
>
> ## Open follow-ups (small)
> - [ ] style LoRA registration (CLI/API) + UI
> - [ ] inline pipeline parameter editor (today: YAML)
> - [ ] OpenAPI response models -> generated web types
> - [ ] real Docker (non-Podman) deployment validation
> - [x] commit Phases 0–2 (3a3f031); legacy line-a containers/images, output/ data, gpu-acceptance project removed
>
> ## Phase 5 — image-kind builds (current)
> Defaults (open Qs): derived material maps off; atlas pow2 = param, default off; sprite pivot bottom_center.
> - [x] B1 build registry (coordinator/builds/*), shared BuildRun scaffolding, mask reuse from QA
> - [x] B2 processing: raster.py (mask→RGBA, trim, pad, pivot, variants, seam, tile), atlas.py (frames, grid pack, meta)
> - [x] B3 recipes: sprite/icon/material build ids, int_list param, structural checks
> - [x] B4 imports: frame sequence (PNGs/zip, safe) → sheet/vfx; material bundle role mapping
> - [x] B5 web: per-kind build/asset viewers (components/outputs.tsx), frame player, import modes
> - [x] B6 tests: unit 12, contract 9, e2e 3, GPU real sprite+icon+material 1 pass
> - [x] B7 docs: SPEC record, acceptance, README, TODO
> - [ ] material tileability: generated materials fail seam gate (ratio ≈4) — decide: seamless-tiling step vs. tiling-capable model/LoRA vs. project threshold
>
> ## Phase 3 — 3D (DINOv3 access granted 2026-09-29)
> Decisions: GPU1 HTTP worker `worker3d` (not ComfyUI-native); clean exporter default (no NVIDIA NC code; torch UV rasteriser
> replaces nvdiffrast in to_glb); opt-in research exporter (upstream nvdiffrast, build arg) → licence not_cleared.
> - [x] verify HF access, download DINOv3 @ lock revision, record sha256, `models verify --full` all ok
> - [x] licence audit: nvdiffrast v0.4.0 + nvdiffrec = research/eval only; TRELLIS.2, CuMesh, FlexGEMM, o-voxel = MIT
> - [x] services/worker3d: /health /generate (→ safe npz raw) /export (clean|research → GLB) /unload (owner ack); previews rendered on Studio CPU
> - [x] clean exporter: torch UV-space rasteriser; nvdiffrast stub in default image
> - [x] Studio: Worker3dClient + fake, GPU1 ownership aux↔3d, model3d build (mask reuse → cutout → generate → export → validate → preview)
> - [x] re-export from raw (F03), triangle budget requested/effective/actual, licence provenance per exporter
> - [x] compose: worker3d on GPU1; remove legacy trellis_worker + compose.legacy-3d.yml
> - [x] tests: contract (fake worker), GPU: real 3D batch, clean vs research exporter on same raw
> - [x] docs (architecture, installation, README, SPEC record, acceptance)
> - [ ] follow-up: GC/retention for raw intermediates (Phase 4); research exporter through a Studio run (needs research image deployed)
>
> ## Later phases
> - Phase 4: S3 backend + conformance, GC plan/execute, retention expiry, export targets (files/Godot/Git)
> - Phase 5: icon/sprite/material/sheet/vfx builds + frame import/packing
> - Phase 6: release validation, offline test, accessibility audit
