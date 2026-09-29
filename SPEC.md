# AssetStudio — General Asset Production and Library
## Refactor specification and implementation guide

**Repository:** <https://github.com/andrejvysny/asset-studio>
**Reviewed commit:** `f5ca95c497318102e397b157815fae5dab6877cc` · **Specification date:** 2026-09-28
**Status:** implementation handoff (received from the product owner). Implementation progress: `TODO.md`.
Decisions and deviations taken during implementation are recorded at the end of this file (§ Implementation record).

> This file condenses the handoff specification into its normative requirements, keeping section numbers and
> acceptance IDs so code, tests and TODO.md can reference them. Where wording matters, the original handoff text wins.

---

## 0. Source authority

Priority: explicit current user requirements → behavioral contracts in this document → companion design (Claude Design
"Asset Studio v2") → existing implementation. Do not preserve a known correctness issue merely to reproduce a mockup.
Destructive migration, credential changes, remote publication, external Git pushes and purges require explicit
operator approval.

## 1. Product definition

A project-based workbench for planning, generating, importing, reviewing, versioning, organizing and exporting digital
assets: a library of actual assets and optional planned entries; a shot list; batch-oriented production (a batch of one
included); human prompt confirmation, candidate approval and final publication; project-defined categories, defaults,
style, QA rules and export settings; local or S3-compatible storage with the same logical layout; self-hosted execution
through ComfyUI and local model/processing services. **No game-specific concepts in application logic.**

Retained constraints: self-hosted inference only (no hosted/cloud fallback); 2 × RTX 4090 (24 GB each, not pooled);
Docker Compose first, Podman via override; AssetStudio is the normal UI, ComfyUI an advanced local UI; AssetStudio owns
the product API and durable operations; Qwen text/image + TRELLIS direction; 4 candidates by default (1–8); advisory QA
with explicit override; BiRefNet before the default image-to-3D recipe; advisory mesh budgets (requested/effective/actual
recorded); local LoRAs, none bundled; explicit model download only; provenance, approved inputs, attempts and versions
preserved.

Scope: one authoritative Studio writer per project; multiple browser tabs; seven kinds — 3D model, Sprite, Icon, VFX
flipbook, Material, Sprite sheet, Concept art. Imported is an origin, not a kind. Recipe states: `ready`,
`missing_models`, `blocked_licence`, `unsupported_configuration`, `experimental`. No simulated success.

## 2. Screens (D01–D12)

Assets · Shot list · Batches · Batch/Approve · Batch/Prompts · Schema · Pipelines · QA rules · Style · Storage ·
Export · Runtime, plus asset detail/version history and build/publication detail. Navigation: LIBRARY (Assets, Shot
list), PRODUCTION (Batches), PROJECT (Schema, Pipelines, QA rules, Style, Storage, Export, Runtime). Header: project,
storage state, actionable waiting count, real GPU status.

Resolutions (§2.3): asset counts exclude planned requests (separate count + Show planned toggle); Imported chip is an
origin filter; "Approve best recommended" is a previewed, confirmed bulk action; "one model load per stage" is a
scheduling objective with actual loads reported; external S3 allowed for storage, never for inference; integrity checks
cannot be disabled/overridden; references claim image conditioning only when an adapter consumes them; publication and
export are separate operations; inherit / explicit value / disabled are distinct; demo data only in an opt-in demo.

## 3. Baseline refactor and carry-forward blockers

Preserve and extend: web stack, approval binding + QA override, advisory QA with coverage, validated IDs and atomic
writes, UV-seam-aware mesh checks, working model integrations, tests. Replace: game catalog/slots, ComfyUI-owned
product state, browser-side workflow patching, legacy CLI/workflows, Podman-first compose.

Blockers: F01 non-commercial exporter in default 3D path · F02 GPU handoff treats errors as release · F03 re-export
without handoff / raw not visible · F04 current attempt vs manifests drift · F05 lineage not resolved consistently ·
F06 restart clears busy without confirming termination · F07 model verifier paths disagree / README-only passes ·
F08 Podman-oriented compose, unfinished locks · F09 GPU test accepts failure outcomes · F10 hardcoded hand-painted
template.

## 4. Architecture and ownership

One Studio control plane owns all mutations, gates, run identities, publication and scheduling. Workers take typed
inputs and return artifacts/events. ComfyUI used via prompt/history/queue/object_info/progress APIs; queue id ≠
completion; interrupt only engine-owned work. Neutral core imports no ComfyUI/CUDA/FastAPI/React. Source of truth:
versioned `studio.yaml`, `shotlist.yaml`, manifests + immutable version records + content-addressed blobs, batch
records; rebuildable local index; local durable operation journal (SQLite not on network filesystems).

## 5. Domain model (entities and invariants)

Entities: Project, Category, ShotListItem, Asset, AssetVersion, Batch, BatchItem, PromptRevision, CandidateSet,
ReviewDecision, BuildRun, Artifact, Operation, ExportRun. Stable internal IDs never contain mutable paths. Kinds:
`model3d · sprite · icon · vfx_flipbook · material · sprite_sheet · concept_art`; origins: generated/imported/derived/
mixed. Distinct notions of version: project revision ≠ prompt revision ≠ candidate set ≠ build attempt ≠ published
version ≠ export profile revision.

Invariants: (1) no image generation without confirming the exact prompt revision; (2) no build without a valid approval
or reviewed import; (3) no publication without user acceptance and structural validation; (4) published content
immutable, failed runs never replace successful ones; (5) approvals bind project, batch, item, set, candidate, image
hash, prompt revision, QA evaluation; (6) config changes never modify existing batches/versions; (7) every durable
output gets an artifact record immediately; (8) identical bytes may dedupe, identities never merge; (9) server state
decides legal actions; (10) no user field is executable.

## 6. Schema, inheritance, snapshots

Visual/YAML editing over one validated schema; value + source shown per field; typed ranges; explicit
`{mode: inherit|value|disabled}`. Resolution: recipe → project → ancestors → category → permitted item overrides;
scalars/lists replace. Snapshot every item's complete effective configuration at batch creation (canonical JSON +
SHA-256). Changes affect new batches only; clone/rebase is explicit. Refuse deleting referenced categories.

## 7. Batches and shot list

Shot list: requests, two-phase CSV/YAML/Markdown import with row errors, idempotent IDs, no LLM repair, optimistic
revisions, derived status, server-side claim checks, split-by-kind. Batch: compatible kind/recipe, persisted seed
family, deterministic cryptographic seeds from stable IDs. Lifecycle per item (§7.3 diagram); tasks `queued, running,
succeeded, failed, cancel_requested, cancelled, blocked, reconciling`; batch views are aggregates with explicit
denominators. Stage passes are bounded, results durable per item; retry ≠ regeneration. Prompt stage preserves original
brief, raw enhancer response, enhanced description, user edit, final positive/negative; revisions immutable once used.

## 8. Review, regeneration, QA

Focus ≠ approval. Approval request binds project/batch/item/set/candidate/sha256/prompt revision/QA evaluation/expected
item revision/idempotency key; stale → 409. Non-recommended requires explicit override (recorded with failed/missing
checks). Build is a separate action on bound approvals. Bulk best: selected undecided eligible rows, preview, deterministic
ranking, per-row outcomes. Regeneration: selected rows only, new revision + set, old history kept, no approval transfer.
QA rules: VLM questions + allowlisted metrics, results pass/fail/unavailable/not_applicable, strict boolean parsing.
Policy: major or ≥2 minor fails → not_recommended; otherwise any unavailable → unverified; zero applicable → unverified;
else recommended. Structural validation is separate and never overridable. Palette/reserved colours are project data
with scopes and tolerances.

## 9. Recipes

Seven fixed families with typed parameters, visible gates, declared capabilities and dependency closures; readiness per
recipe. Matrix: model3d (BiRefNet → TRELLIS → validation, textured GLB), icon, sprite, concept (no forced object
constraints), material (labelled derived maps), sheet and vfx (verified temporal adapters only; frame import otherwise).
No fake animation/PBR/conditioning. Models: Qwen3-VL-8B, Qwen-Image-2512, BiRefNet, TRELLIS.2 (native TRELLIS-only
ComfyUI spike with licence/memory/artifact evidence; no Pixal3D branch). 3D preprocessing, conservative cleanup, UV-seam
safe connectivity, delivered-GLB validation and final previews; versioned safe raw intermediates.

## 10. Style, references, LoRAs

Optional style profiles (guide feeds enhancer/QA, not pasted verbatim), palettes with scopes, immutable reference sets
with explicit modes (prompt guidance / image conditioning / QA reference; block rather than silently downgrade), LoRAs
registered with hash, base, triggers, licence; `none`, disabled and strength 0.0 are real values; no runtime downloads.

## 11. Library, imports, publication

Assets view from manifests (search, descendants, combinable filters; planned tiles without fake versions). Asset detail
with version timeline, provenance, validation/QA, explicit set-current with audit. Imports: PNG/JPEG, self-contained GLB,
material bundles, frame sequences; inspect → map → validate → explicit accept; reject traversal, external URIs, bombs,
pickles; unknown licence stays unknown. Publication: exact build IDs, expected current versions, idempotency; per-asset
commit; manifest commit is the publication point. Version manifest minimum fields per §11.4.

## 12. Scheduling, GPUs, recovery

GPU0 image engine; GPU1 text/VLM/segmentation/3D with explicit ownership; CPU for metadata/packing/exports. Model-
affinity passes; release residency at human gates. Ownership handoff requires explicit acknowledgement; timeouts,
resets, HTTP errors, malformed bodies or `loaded=true` never grant ownership. Durable journal, 202 + operation ID,
outbox/reconciliation, at-least-once execution with deduplicated commits; reconcile lost submissions before resubmitting;
targeted cancellation only; restart → `reconciling`; no silent OOM fallbacks; advanced ComfyUI use must not race the
scheduler.

## 13–15. Storage, S3, offline, retention, GC

Logical layout (§13.1); repository operations (read with version token, create-if-absent, replace-if-version, list,
stat, verified blob write/open, GC plan/execute); atomic local writes; S3 conditional writes (ETag ≠ SHA-256);
publication sequence (§13.3); rebuildable index; pagination; measured statistics. S3: credentials never in project files,
sentinel-only connection test, single writer with fencing, explicit migrations, precise storage states. Offline:
everything local works without internet; bundle all frontend assets; test with egress blocked. Retention defaults keep
everything; GC is plan → preview → confirm → execute with grace periods.

## 16. Export targets

Export materializes published versions via frozen plans (allowlisted variables, sanitized paths, collision detection,
managed files only). Plain files + manifest (required), optional Godot (tested importer settings), folder/Git (manual,
managed files only, no implicit push). Publication and export succeed/fail independently.

## 17. Runtime, model verification, licences

Runtime shows real GPUs, workers, residency, readiness, integrity and licence status separately. One model lock +
closure verifier for download, verification, readiness and provenance (reject empty/partial/README-only, wrong sizes,
corrupt weights, path escapes). Licence status attaches to artifact/run provenance and is never rewritten retroactively.

## 18. API

FastAPI/OpenAPI, typed requests; domain endpoints only (§18.2 families); idempotency keys + expected revisions;
400/404/409/422/503 with stable codes; resumable events with polling fallback; versioned engine bindings validated
against the pinned engine.

## 19–20. Frontend, security, deployment

Integrate the design's layout with the typed API; demo only in explicit mode; all buttons real or disabled with a reason;
missing states (empty, unavailable, corrupt, missing model, stale, busy, disconnected…); accessibility. Loopback,
same-origin/CSRF protection, untrusted input handling, resource limits, Docker-first compose with GPU reservations and
a Podman override, non-root, read-only model mounts, pinned builds, operator commands (§20.4).

## 21. Legacy migration

(Superseded by decision: legacy data discarded — see Implementation record.)

## 22. Acceptance matrix

G01–G06 generality/schema · I01–I04 imports · B01–B08 batches · Q01–Q06 QA · M01–M07 3D · P01–P05 publication ·
S01–S09 storage · R01–R07 recovery/runtime · E01–E03 exports · U01–U03 UI. Recipe-specific tests per kind (§22.2).
Real-environment validation on the target host (§22.3). A skipped GPU test is "not run", not "passed".

## 23. Phases

0 baseline · 1 core/schema/local repository · 2 Studio-owned batches + real image production · 3 permitted 3D engine,
GPU scheduler, publication · 4 S3, retention, exports · 5 seven-kind workbench · 6 migration, parity, release validation.

## 24. Definition of done

Clone, deploy, create an empty general project, define categories/style/QA without code changes, import or plan
assets, batch, confirm prompts, review exact candidates, build only an approved subset, regenerate selected rows,
inspect actual results, publish immutable versions, export through configured targets; local or verified S3 storage;
portable data; self-hosted generation; offline local acceptance; recoverable failures; no default game concepts.

---

## Implementation record (decisions during implementation)

| Date | Decision |
|---|---|
| 2026-09-28 | This implementation run covers Phases 0–2 as one vertical slice; later-phase controls are disabled with reasons. |
| 2026-09-28 | Docker is the baseline; `compose.podman.yml` override; validation on this host uses Podman 4.9 + podman-compose 1.0.6 (Docker not installed: "not run"). |
| 2026-09-28 | Legacy Line A data and catalog discarded (no migration tooling); `output/` left on disk, untracked. |
| 2026-09-28 | 3D uses TRELLIS.2 + DINOv3 only (no fallback model). DINOv3 access is pending; weights are not downloaded; `model3d` build shows `missing_models`. |
| 2026-09-28 | Concept art gets a passthrough build (approved original → final + preview) to exercise the full lifecycle in Phase 2. |
| 2026-09-28 | BiRefNet moved into the aux service (GPU1 text/VLM/segmentation); the legacy TRELLIS worker is only reachable through `compose.legacy-3d.yml`. |
| 2026-09-28 | API response types are hand-written in `web/src/lib/api.ts` (most responses are view dicts); OpenAPI type generation deferred until response models exist. |
| 2026-09-29 | Phase 5 (image kinds) before Phase 3 while DINOv3 is pending. Sprite/icon/material builds; sheet/VFX only from imported frame sequences (no verified temporal generator). |
| 2026-09-29 | Material builds publish the approved base colour + tiling preview only; derived PBR maps are not generated (no verified local derivation). Full map sets come from material-bundle imports with explicit per-file roles. |
| 2026-09-29 | Material seam check = wrap-edge ΔE ÷ neighbour ΔE (`seam_max_ratio`, default 2.0), structural and not overridable. |
| 2026-09-29 | Sprite/icon cut-outs reuse the QA mask only when its lineage is the approved candidate artifact; otherwise the build segments on GPU1 (build op runs on the gpu1 lane). Sprite pivot default `bottom_center`; atlas power-of-two is a parameter (default off). |
| 2026-09-29 | DINOv3 access granted; weights downloaded at the locked revision and hash-verified. |
| 2026-09-29 | 3D runs in a stateless GPU1 HTTP worker (`worker3d`), not a second ComfyUI: same ownership/ack contract as aux, no product state. |
| 2026-09-29 | F01: default GLB exporter ships no NVIDIA non-commercial code (PyTorch UV rasteriser in an o-voxel `to_glb` port). Upstream nvdiffrast exporter is opt-in (`RESEARCH_EXPORTER=1`, `exporter: research`) and recorded `not_cleared`. |
| 2026-09-29 | Raw TRELLIS.2 output is stored as a pickle-free `.npz` artifact (retention `raw`, ~100–300 MB) for re-export; intermediates are not version files. `cutout_padding`/`cleanup` params removed (TRELLIS.2 re-crops to alpha; upstream cleanup is not switchable), `remesh`/`exporter`/`pipeline_type`/`triangles` added. |
