# TODO — Build profiles + GPU check (2026-09-30)

Plan: ~/.claude/plans/ultra-snappy-castle.md (spec Phase 2 slice). Owner: typed build_profiles (material CPU stage with
texture rewrite + worker geometry params), real-GPU A/B on rock/prop/tree; ride-alongs: effects style preview, media
fixes, renderer accuracy. No image->3D fast path, no Godot.

- [x] 1 profiles core: config, validation, snapshot, effects, config:effects?style=
- [x] 2 material stage: materials.py (JSON + texture rewrite), finalize checkpoint, checks, material-only rebuild
- [x] 3 worker geometry policy: params, to_glb, health feature, preflight, worker tests
- [x] 4 web: profile editor, reference select, rebuild fields, style preview, media restore/download
- [x] 5 renderer: linear factor, real BLEND
- [x] 6 GPU acceptance: rebuild stack, rock/prop/tree A/B, docs/acceptance.md
- [x] 7 docs

Result: lint clean, 448+ backend + 36 e2e pass, worker3d tests pass; real GPU `test_model3d_profiles` 1 pass (9 m 19 s).
Follow-ups:
- [ ] tests/gpu/test_model3d.py waits on ops_idle only (same race fixed in test_model3d_profiles): switch to item state
- [ ] calibrate `auto` alpha threshold on more assets; decode-time fill_holes needs a TRELLIS decode adapter
- [ ] single_component check is weak for generated organic assets (rocks = thousands of islands)
- [ ] per-part material bindings (profile applies to the whole asset); image->3D fast path; Godot runtime validation

# TODO — Style correctness + lean profiles (2026-09-30)

Plan: ~/.claude/plans/ultra-snappy-castle.md (spec "Configurable Game Styles and 3D Production", Phase 0 + lean Phase 1).
Owner: project reference sets route for new snapshots only; enhancer order source > item refs > project set, excluded
recorded; no StylePackage/5-profile/schema v2 yet.

- [x] 0a regression tests (FakeAux captures inputs + 4-image cap): set routing, qa_reference, image_conditioning 422,
      legacy snapshot inert, variant source + 4 refs
- [x] 0b reference_bindings resolver, `reference_routing` snapshot marker, prompt/qa_compare wiring, admission block
- [x] 0c effects.py + config:effects / item effects endpoints + non-fatal config warnings
- [x] 1a style revisions store + history API
- [x] 1b web: multi-style, history, effect panel, labels, set refs in Job refs panel, "no effect yet" markers
- [x] 1c docs (SPEC, architecture, style-effects.md), e2e, MCP read tools (config_effects, style_history)

Result: lint clean, 410 backend + 33 e2e pass, web build ok (SIMULATED engines; no GPU run needed).
Follow-ups:
- [ ] spec Phase 2: exporter alpha mode (mask/blend + cutoff) + independent double_sided; typed geometry cleanup
- [ ] effects panel follows the scope's resolved style, not the selected style chip

# TODO — MCP server for remote agents (2026-09-30)

Plan: ~/.claude/plans/act-as-senior-software-sunny-bonbon.md. Owner: agents pass all gates (actor recorded), in-process
listener :8191, bearer tokens (read/full), inline ≤16 MB + signed URLs. Facade over REST via ASGITransport.

- [x] Phase 0 spike: mcp==1.30.0; FastMCP streamable HTTP on 2nd uvicorn server + auth middleware; contextvar through
      ASGITransport into sync endpoints; test harness over ASGITransport + session_manager.run()
- [x] Phase 1 foundation: settings, listener (:8191, clean SIGINT), token store + `assetstudio mcp create|list|revoke`,
      StudioClient + error mapping, actor plumbing (intent plan -> decisions/waves), studio tools, guide resources,
      file spool + signed URLs, views/binding; test_mcp_foundation (5)
- [x] Phase 2 config/library/media tools (Sonnet impl, reviewed; import cap -> max_upload_bytes, images downscaled
      for vision), test_mcp_config_library (7)
- [x] Phase 3 jobs + gates (auto-binding), wait_for_job, batches/runs, ops (Sonnet impl, reviewed; fixed pinned
      prompt scope, build `status` field), test_mcp_production (6, incl. actor on decisions + intent)
- [x] Phase 4 variants, studio_api, produce_asset prompt, docs/mcp.md, compose/Dockerfile port, README/SPEC/arch

Result: lint clean, 388 backend tests pass (19 MCP). Live smoke: real server + MCP client, fake engine, create project
-> config -> job -> all gates -> publish -> signed download sha ok. Not committed.
Follow-ups:
- [ ] 75 tools: consider consolidating (clients with tool limits); variant tools + item_reference/reexport untested
- [ ] OAuth 2.1 (needed for claude.ai custom connectors); token UI panel in Runtime
- [ ] UI: show decision actor (operator vs agent:<name>) in history
- [ ] real-GPU run driven through MCP

# TODO — Media Library (2026-09-29)

Plan: ~/.claude/plans/do-thorough-analysis-of-toasty-iverson.md. Owner: images only (PNG/JPEG/WebP), flat + search + tags,
guidance-only refs, soft archive, reference uploads auto-added, picker in New Job/Job refs/Style + "New Job from media".

- [x] Phase 1 backend: `med` id, MediaItem, storage/media.py, services/media.py, routers/media.py, references media_id,
      references:upload auto-add, counts.media, contract tests
- [x] Phase 2 web: mediaApi, Media + MediaPreview screens, MediaPicker, NewJob/ReferencesPanel/Style wiring, nav/route
- [x] Phase 3 e2e test_ui_media.py + docs (architecture source-of-truth, SPEC record)

Result: lint clean, 369 backend + 29 e2e pass, web build ok. Not committed.
Follow-ups:
- [ ] re-upload of an archived item reports "duplicate" but stays hidden (offer restore)
- [ ] download filename lacks extension (`reference-<sha12>`); use media name + format

# TODO — Hardening pass (review of 8de5fe8, 2026-09-29)

Review: "AssetStudio — Master review, correctness findings and hardening plan" (R01–R11). All 11 findings
confirmed against source at 65c16ab. Scope: WP-A (review/run invariants) + WP-B (variant inputs) + R07/R10/R11.
WP-C (GPU/operational re-run), WP-D (files+manifest exporter), WP-E (project style, materials) are NOT in this pass.

- [x] R01 build/approval binding: approve clears or restores the matching build; accept checks the build's own
      candidate binding; attempt labels come from each run's decision; historical accept disabled (UI = API)
- [x] R08 approve + accept_builds through commands.execute (planned per-unit decisions, idempotent replay)
- [x] R02 persisted run control (run/paused/cancelled/closed) enforced at create/ready/claim/retry; wave
      membership + openness validated before effects
- [x] R03 retry/claim use the same single-owner rule as create (typed 409)
- [x] R04 deferred downstream: savepoint (no partial chains), re-admitted on owner release + periodic reconcile
- [x] R05 prepared-input profile v2 (decode, EXIF transpose, sRGB, alpha composite, always PNG)
- [x] R06 renderer v2: base colour factor, texture alpha, MASK/BLEND cutout, two-sided lighting, extension checks
- [x] R09 edit workflow readiness in variant capability; exporter override checked in build preflight
- [x] R07 execution-time model/workflow/licence receipts; derived licence status never better than source
- [x] R10 worker3d executor survives spool errors, liveness in health; GPU acquisition failures visible + bounded
- [x] R11 index rebuild keeps concurrent upserts; grouped cursor revision read with its page
- [x] §5 source binding compares content identity (rename-safe); machine_enforced constraints refused;
      task list reads unbounded for run/item control
- [x] docs: acceptance scope matrix (tested / not hardware-validated / experimental / deferred) with exact commits

Result: lint clean, 358 backend + 28 e2e pass, web build ok (SIMULATED engines; no GPU re-run of this pass).
Follow-ups (not in this pass):
- [ ] WP-C: real-GPU + operational re-run at the hardened head (JPEG 2D source, alpha-tested foliage, worker loss,
      disk-full spool, 30–60 min mixed load); egress-blocked edit-model run (keeps generative variants experimental)
- [ ] WP-D: files + manifest exporter (server folder + browser ZIP from one frozen bundle), bulk tag/category, version compare
- [ ] WP-E: project style wizard + revisions; Surface/Seamless materials + seam repair
- [ ] "QA queued" label only shows once the older QA finished; while it runs the item shows "QA running"
- [ ] run summary counts still mix current item state with run tasks (separate run receipts)
- [ ] renderer: factor applied in sRGB space; grey-ICC images refused; BLEND approximated as MASK 0.5

# TODO — Asset Variants & Families + Claude Design v2 UI (milestone 2026-09-29)

Spec: owner guide "AssetStudio — Asset Variants and Families" (baseline 60ff832; started at 0310b08).
Design: Claude Design project d7c3ecf3 "Asset Studio v2.dc.html". Owner decisions: design wins on Job granularity
(one Job = one asset; Create variants -> N one-item Jobs + draft Batch sharing one VariantPlan); full design UI now,
refs + Conservative/Creative real; Qwen-Image-Edit-2511 download + GPU spike approved; commit per green phase.
Baseline: lint clean, 130 tests pass.

## Phase A — domain, families, library index, publication
- [x] core/variants.py (methods, intents, transforms, rows, SourceBinding, VariantPlan/Draft/Context, Derivation,
      capabilities, row validation); domain: AssetFamily, manifest.family_id, version.derivation, Job.variant/direct,
      JobItem.references/enhance_preset; id prefixes
- [x] storage: families store + source attach (CAS), family-aware publication (new asset joins family, derivation,
      origin=derived), no duplicate publish of same accepted build
- [x] index: family_id + family name search, family filter, server-side group-by-family pagination, rebuild
- [x] library API: asset detail family/derivation, families GET/PATCH, assets ?family=&group_by=family
- [x] processing: GLB parent-transform scale (bounds, anchors, preservation checks), raster resize/pad, single-view
      source render (no montage), tests VT01–VT08
## Phase B — variant drafts -> Jobs (+ direct vertical slice)
- [x] source binding/eligibility preflight (capabilities endpoint, reasons), source references (renders/2D prep)
- [x] drafts CRUD (revision), create-jobs: plan freeze, family resolve, N one-item Jobs + draft Batch, idempotent
- [x] direct build route (no prompt/candidates; confirmation binds numeric transform), final sizing helper
- [x] tests VD01–VD08/VD11, VP01–VP04, VT05/VT08/VT13 (fake engines); VL via families contract
- [x] aux v2: /enhance presets+edit+images, /compare, /analyze_source, /suggest_variants (live-smoked)
## Phase C — planning, references, style
- [x] draft-scoped VLM tasks: analyze source, suggest rows (never overwrite manual rows)
- [x] style/source conflict + ack (409 style_source_conflict)
- [ ] preserve/change enforcement preview table (UI; machine/advisory/unsupported)
- [x] Job references (upload/library, note, crop) + Conservative/Creative enhancement (aux enhance v2)
## Phase D — image editing (Qwen-Image-Edit-2511)
- [x] lock entry qwen_image_edit_2511 (fp8mixed, Comfy-Org @f68ace8), download started
- [x] edit graph + bindings (template 0.11.69 non-Lightning); canary: source conditioning verified, ~130 s/edit @40 steps, GPU0 peak 24.07 GB, alpha must be composited on declared bg
- [x] ImageEditRequest, workflow registry, controlled upload, edit graph + bindings, fake edit engine
- [ ] GPU spike: conditioning canary, VRAM/RAM, offline, T2I<->edit switch (VG01–VG12)
## Phase E — generative variants + QA
- [x] generate stage edit mode (source-conditioned, never sibling), rounds (approve from any set)
- [x] variant QA dims (resemblance/change/style), diversity report on selection, final sizing after rebuild
- [x] build modes retry/resample/rebuild; final sizing after rebuild
- [x] recovery VR01–VR08 (+VR06b orphan prompt cancel), scheduling VS01–VS04, migration VM01–VM03; fixes: lost QA downstream, cancelled generate re-run, backoff race (flaky test), orphaned prompt cancel
- [x] 2026-09-29 owner request: consolidated all projects into Demo 3D (6 assets republished w/ provenance; 4 projects removed; backups ~/assetstudio-backups/2026-09-29-consolidate)
## Phase F — UI to design v2 + delivery
- [x] Assets group-by-family + family filter; Asset detail family/derivation + New variant/Create variants
- [x] Create variants wizard; Job detail (3 tabs, rounds, refs, variant card, direct card, retry menu, stepper)
- [x] Jobs list (group-by, pills, batch/rnd, run standalone); Batches (new, gates, 3-tab detail inline review)
- [x] New Job single asset; Shot list one Job per row + Batch toggle
- [ ] e2e, docs (SPEC, README, architecture, acceptance), real GPU acceptance (pine + non-plant + icon + direct)

### Known limitations / follow-ups
- Second Batch start is blocked while one of its runs is open.
- No bundle download endpoint (per-file downloads only).
- Export manifest variant extension pending the shared exporter (Jobs/Batches Phase 3).
- Jobs/Batches Phases 3–6 still open (below).
- Preserve/change enforcement preview table (UI) not implemented.
- Project-style revisions (Phase 4) not implemented: style conflict uses snapshot style hashes.

---
# Previous milestone (Jobs/Batches) — Phases 3–6 still open
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
- [x] journal StageTasks/passes/command intents H05 (one-owner-per-item-family instead of a reservations table)
- [x] publication: role contracts, authoritative names, receipts IM01 IM02 RI16
- [x] imports replay-safe + upload ids + decoded budgets H08 H17 RI15 IM04 IM05
- [x] BuildRun checkpoints + attach-at-create + isolated preview H01 H15 RI07–RI09
- [x] model3d target precedence H12 RI18, preflight H18, recipe compat H03
- [x] worker3d: lease/session/epoch, activity drain, executions + spool H02 RI10–RI12
- [x] raw_npz CPU validation H13 IM06; rasterizer winner + tiling H14 IM07 (make test-worker3d)
- [x] aux: exec id, lease, drain
- [x] bounded auto-retry H16, lane survives bugs RI17
- [x] failure taxonomy per StageTask (coordinator/errors.py)

## Phase 2 — Jobs + Batch execution
- [x] Job services (save-only create), Batch groups, run planner, runs + waves
- [x] StageTasks per stage, pass runner, coalescing, fairness, atomic downstream, startup reconciliation
- [x] v2 routers (jobs, batches, runs, tasks, passes) + v1 adapters; SSE events carry job/run ids
- [x] CLI: jobs list, batches plan/start, operations inspect/retry/cancel (via the running Studio)
- [x] web: Jobs rename, Batches screens (list/detail/plan/run view with waves), nav, legacy redirects, Runtime passes
- [x] tests JB01–JB16, RI04, RI05, IM10, e2e multi-Job (tests/e2e/test_ui_batches.py)
- [ ] OpenAPI typegen (EX14) → Phase 6
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
