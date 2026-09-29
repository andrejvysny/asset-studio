# TODO — AssetStudio refactor

Spec: SPEC.md (AssetStudio general spec, 2026-09-28). Plan: ~/.claude/plans/act-as-senior-software-iridescent-zephyr.md
Branch: refactor/assetstudio-core. This run: Phases 0–2 (vertical slice). Decisions: legacy data discarded; Docker
baseline + Podman override; 3D = TRELLIS.2 + DINOv3 only (no fallback; DINOv3 access pending, not downloaded).

## Phase 0 — baseline
- [x] baseline f5ca95c: 82 unit tests pass; host docker = podman wrapper; 2x4090; 1 legacy job (discarded)
- [x] design import (Asset Studio v2.dc.html) reviewed; support.js = dc runtime only

## Phase 1 — core, storage, import, publish
- [x] packages/assetstudio_core: ids, kinds/origin, config + inherit/value/disabled, snapshots, QA policy, recipes, naming, seeds, lifecycle, review binding, shot-list parsers
- [x] packages/assetstudio_storage: Repository, LocalBackend (atomic, link-CAS, blobs), ProjectStore, publication (idempotent, derived ids), SQLite index (shadow rebuild), writer lock
- [x] packages/assetstudio_processing: image inspect/thumbnail, mask/palette metrics, GLB container + mesh validation
- [x] models.lock.yaml + closure verifier (F07 fixed: DINOv3 README-only -> pending_access)
- [x] Studio API: projects, config (visual/yaml, revision), assets/versions/set-current, imports, artifacts, storage view/test/reindex, runtime, capabilities
- [x] CLI: doctor, models verify [--full], project create/list/register, storage reindex/verify
- [x] tests: unit (core/storage/runtime/mesh) + contract (library)

## Phase 2 — batches + real image production
- [x] shot list CRUD + CSV/YAML/MD two-phase import, derived status, claim checks
- [x] batches/items, prompt revisions, confirm gate, regenerate, approve (exact binding), preview-best, accept, publish
- [x] journal (held→queued, idempotency, reconcile on restart), coordinator lanes, GPU1 ownership w/ ack
- [x] ComfyUI adapter (stock nodes, bindings, deterministic prompt ids, targeted cancel); aux service rewrite (enhance/qa/cutout/unload ack)
- [x] concept passthrough build; 3D build blocked with reason
- [x] tests: contract batches + recovery (simulated engine)
- [x] web/: v2 design screens wired to API (12 screens + asset detail + new batch + 5-tab batch workspace)
- [x] e2e Playwright vs real Studio process (simulated engine): 13 pass; offline check (decoders bundled)
- [x] GPU acceptance (real 7-item concept batch, restart mid-pass, 28 exact executions, publish, hash verify): pass
- [x] docs: README, docs/architecture.md, docs/installation.md, docs/acceptance.md; SPEC.md (condensed + decisions)

## Open follow-ups (small)
- [ ] style LoRA registration (CLI/API) + UI
- [ ] inline pipeline parameter editor (today: YAML)
- [ ] OpenAPI response models -> generated web types
- [ ] real Docker (non-Podman) deployment validation
- [ ] commit branch (awaiting user)

## Later phases (not this run)
- Phase 3: native ComfyUI TRELLIS.2 worker (needs DINOv3 access), 3D build/re-export, mesh processing
- Phase 4: S3 backend + conformance, GC plan/execute, retention expiry, export targets (files/Godot/Git)
- Phase 5: icon/sprite/material/sheet/vfx builds + frame import/packing
- Phase 6: release validation, offline test, accessibility audit
