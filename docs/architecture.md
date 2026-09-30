# Architecture and data ownership

## Components

```
browser ──HTTP/SSE──> studio (FastAPI) ── coordinator lanes ──> comfyui (GPU0)   stock nodes, versioned graph
                         │                                   ├─> aux (GPU1)      enhance / VLM QA / BiRefNet
                         │                                   └─> worker3d (GPU1) TRELLIS.2 + DINOv3, GLB export
                         ├── ProjectStore ──> project root (portable: YAML + JSON + blobs/sha256)
                         ├── AssetIndex (SQLite, instance dir, rebuildable)
                         └── Journal (SQLite, instance dir: stage tasks, model passes, command intents,
                                      scoped idempotency, GPU lease epochs; backed up with the project)
```

Dependency direction: `assetstudio_core` ← `assetstudio_storage` ← `assetstudio_processing` ← `assetstudio_server`.
The core imports no FastAPI, CUDA or ComfyUI.

| Package | Responsibility |
|---|---|
| `packages/assetstudio_core` | ids, kinds/origins, `studio.yaml` schema (inherit/value/disabled), inheritance + snapshots, recipes, QA policy, review binding, lifecycle aggregation, naming, seeds, shot-list parsers |
| `packages/assetstudio_storage` | `Repository` contract, `LocalBackend` (atomic writes, link-based create-if-absent, content-addressed blobs), `ProjectStore`, publication, index, writer lock |
| `packages/assetstudio_processing` | safe image inspection, thumbnails, mask/palette metrics, GLB container + mesh validation |
| `services/studio/assetstudio_server` | API routers, services, coordinator + task handlers, engine adapters, runtime/model verification, CLI |
| `services/prompt_service` | aux inference service (bytes in, results out; no files, no product state) |
| `services/worker3d` | TRELLIS.2 sampling → pickle-free `.npz` raw; GLB export (clean or research rasteriser); stateless |

## Source of truth

| Data | Where |
|---|---|
| project configuration | `studio.yaml` (revisioned; every edit names its expected revision) |
| effective config per Job item | `config/revisions/<sha256>.json` (immutable snapshot) |
| requested assets | `shotlist.yaml` (status derived from Jobs + publications) |
| assets and versions | `manifests/<asset>.json` (current pointer, conditional update) + `versions/<asset>/<ver>.json` (immutable) |
| bytes | `blobs/sha256/ab/cd/<sha256>` (immutable, deduplicated) + `artifacts/<id>.json` (role, lineage, provenance) |
| Job history | `jobs/<job_…>/{job.json,items,prompts,candidates,qa,decisions,builds}`; Jobs created before the rename keep their `bat_…` id under `batches/<bat_…>/{batch.json,…}` (read in place, never moved) |
| Batches (groups of Jobs) | `execution_batches/<bch_…>/batch.json` (membership, revision, run ids) |
| runs, plans, waves | `runs/<brn_…>.json` (frozen selection), `runs/plans/<sel_…>.json` (frozen, hashed), `waves/<wav_…>.json` |
| readable names | `names/<name_id>.json` (atomic create-if-absent: authoritative uniqueness; the index is only a cache) |
| media library | `media/<med_…>.json` (id derived from content sha256: same bytes = same item; name, note, tags, source rights/URL, `archived_at`, revision) → `reference` + `preview` artifacts. Every `references:upload` lands here. Guidance only; archive hides, never deletes (Job refs keep working). A future blob GC must treat these artifacts as roots |
| style history | `styles/<style_id>/<sha256>.json` (immutable, one per distinct content; sha = the prompt revision's `style_sha`; written after each config save, backfilled on read). `studio.yaml` stays the authority |
| publication / import receipts | `publications/<op>.json`, `imports/<imp>.json` (written before any cleanup) |
| live dispatch | instance journal (SQLite, same host only): the ONLY authority for task state |
| search | instance index (SQLite, rebuilt from manifests: `storage:rebuild-index`) |

## Jobs, Batches, runs

- **Job** = a configured production workflow of one or more items of one kind/recipe (formerly "batch"). Saving a
  Job never starts inference. `Save and run` / `:run` starts a *standalone run* through the same planner.
- **Batch** = a named group of Jobs of one project. It owns no item content, history, style or approvals; editing it
  never starts inference and affects only the NEXT run.
- **Run** = a frozen selection: `:plan` stores a hashed plan (what would be enhanced / waits at a gate / is excluded
  and why); `:start` with that plan id + hash creates the run (id derived from the plan, so starting it twice yields
  one run). Starting authorizes enhancement only, up to the first human gate.
- **Waves** = gate actions on a run across Jobs (`:confirm-prompts`, `:approve-candidates`, `:build-approved`,
  `:accept-builds`, `:publish`). Each binds the exact then-current revisions; every unit still gets its own immutable
  decision record. Unselected rows stay where they are and can join a later wave or a later run.
- A Job in an open run cannot be started elsewhere (409); its Job-level actions become scoped continuations of that run.

## Style and reference routing

A Job item freezes its effective configuration in a snapshot. What each field is planned to do is reported by
`assetstudio_core.effects.field_effects` (`GET …/config:effects` for a new Job, `GET …/jobs/{j}/items/{i}/effects` for an
existing item): `applied`, `conditioning_only` (sent to a model, compliance not guaranteed), `advisory_only`,
`not_applicable` or `unsupported` (nothing reads it). This is the planned mechanism, not execution evidence. Config
responses list set-but-inert fields as non-fatal `warnings`. Field-by-field trace: `docs/style-effects.md`.

References are selected by `services/reference_bindings.resolve_references` for both consumers: item references first,
then the snapshot's project reference set when its mode names the consumer (`prompt_guidance` → enhancer,
`qa_reference` → compare QA). The enhancer takes at most 4 images and a variant's source uses one of them. Everything
left out is recorded with a reason in the prompt revision's `references_excluded` (and in compare-QA inputs). A set in
`image_conditioning` mode blocks generation (`reference_conditioning_unavailable`). Snapshots written before routing
existed (no `reference_routing` key) never route their set; their images are listed as excluded.

## Lifecycle

Per item, never Job-wide: brief → enhanced prompt revision → **human confirms exact revision** → candidate set
(+ advisory QA) → **human approves exact candidate** (set id, candidate id, sha256 re-hashed from stored bytes, prompt
revision, QA evaluation, item revision) → build run (attached to the item when created) → **human accepts final
result** (structural validation mandatory) → publication (per asset, idempotent, derived ids, receipt). Regeneration
creates a new prompt revision and candidate set for selected rows only; history is never overwritten.

## Scheduling (stage-first, model-aware, across Jobs)

- Work is a **StageTask** per (item, stage, exact inputs): `enhance`, `generate`, `mask`, `qa_vlm`, `qa_compare`, `qa_finalize`,
  `segment`, `sample`, `bake`, `finalize`, `derive`, `preview`, `publish`. Logical keys make creation idempotent.
- Each task carries a **residency signature**: backend + exact model identities from the pinned lock + weight-changing
  modifiers (e.g. `comfyui:qwen_image_2512@…|speed=lightning_8step@…`, `aux.vlm:…`, `worker3d.trellis:…`,
  `worker3d.bake:clean`). Prompts, seeds, kinds, categories and Job ids are never part of it.
- Lanes `gpu0`, `gpu1` (one thread each) and `cpu` (bounded pool) repeatedly pick a residency group of ready tasks
  across ALL Jobs/runs and execute it as one **ModelPass** under one resource grant. A lone Batch never reloads a model
  because it crossed a Job boundary. GPU0 and GPU1 run independently; GPU1 never overlaps two owners.
- Coalescing: `mask`/`qa_vlm` wait for a window (8 tasks), until no generation is pending, or 45 s — so QA is one
  BiRefNet pass and one VLM pass over many candidates instead of alternating models per candidate.
- Fairness: at most 4 consecutive passes of one residency while others wait; ≤64 tasks and ≤30 min per pass;
  boundaries fall between tasks, never inside one. 3D builds decompose into segment → sample → bake → finalize, so
  approved 3D items of several Jobs are segmented, then sampled, then baked in grouped passes.
- Evidence: each pass records its lane, residency, tasks, Jobs, close reason, and the worker's model-load counters
  before/after (`aux`: vlm/birefnet, `worker3d`: trellis2). ComfyUI exposes no load counter: shown as *unavailable*.
  Resource grants (lane history) are reported separately from model loads.

## Recovery

- **Journal is the only task authority.** Items store product outcomes only; the item view derives task state from
  the journal (legacy items fall back to their recorded refs), so there is no item/journal dual-write gap.
- **Commands**: validate + plan (no writes) → durable intent → idempotent effects (derived ids, logical task keys,
  guarded item mutations) → recorded response. Intents left open by a crash are replayed at startup. Idempotency keys
  are scoped by project + action and bound to the whole request (including target ids).
- **Downstream**: a stage's success and its required follow-up tasks (e.g. generation → QA) commit in ONE journal
  transaction (inside a savepoint, so a chain is all-or-nothing); startup also repairs any succeeded task whose
  downstream is not marked created. A chain deferred behind an older owner of the same item stage is re-admitted
  after every task outcome and on each retry-loop tick (no restart needed).
- **Cancel/pause**: intent (`control`) is separate from execution state, survives restarts and is never cleared by a
  retry; state transitions are conditional. Cancelling a run affects only its tasks. Run-level intent
  (`run_controls`: run/paused/cancelled/closed) is the admission authority: tasks of a paused run are created paused
  and never dispatched, a cancelled/closed run admits nothing (`run_not_open`). Wave routes validate run openness and
  frozen selection before any effect.
- **Ownership**: one active owner per item stage family, enforced by create, retry (409 `busy`) and claim.
- **Review binding**: a build attempt belongs to the candidate decision it was built from; acceptance and publication
  require it to match the current approval's candidate. Re-approving a candidate restores its latest attempt.
- **Retries**: explicit retry = same logical inputs; automatic retries of transiently blocked work are bounded
  (6 attempts / 2 h) and back off per resource; an explicit retry bypasses the backoff. GPU1 acquisition failures
  are recorded on waiting tasks (`progress.admission`) and block them after 6 consecutive failures.
- **Failure scope** (`coordinator/errors.py`): invalid input/output → that item fails, others continue; engine
  unavailable / ownership unknown → the resource blocks (pass stops, other lanes continue); corrupt artifact → blocks
  dependants with an explicit repair action; unexpected exception → that task fails with its type, lane keeps serving.
- **Engines**: ComfyUI prompts use deterministic ids (looked up before any resubmission). worker3d executions use
  ids chosen and persisted by the Studio before submission; results are spooled on a persistent volume until the
  Studio acknowledges durable ingestion; work that died with the worker is `lost` and needs an explicit new build.
- **GPU1 ownership**: a grant uses a new monotonic epoch (persisted). Other workers must drain — stop admitting, wait
  until NO GPU work is queued or running (sampling, export, transfers) — and acknowledge
  `{loaded: false, active: 0, admitting: false, epoch, owner_token}`. Anything else (timeout, reset, malformed, stale
  epoch, active work) leaves ownership unknown until an operator reset. Requests carry the epoch; stale ones get 409.

## 3D path (model3d.default)

approved candidate → foreground mask (QA mask reused when its lineage is the approved artifact, else BiRefNet on GPU1)
→ RGBA cut-out → worker3d `generate` execution (TRELLIS.2 `1024_cascade` by default, DINOv3 image encoder) → raw
intermediate validated on CPU (`assetstudio_processing/raw_npz.py`, shared with the worker) and stored as a `raw`
artifact (retention `raw`) → worker3d `export` execution → GLB → structural checks on the delivered bytes (container, reload, finite
vertices, indices, UVs, base-colour texture) + advisory triangle budget (requested → effective → actual) → CPU preview
(4 views, `assetstudio_processing.render`; a preview failure never invalidates the model and can be retried alone)
→ final human accept → publish. With a build profile, its geometry policy (`small_components`,
`fill_holes`) is passed to the export, and a CPU **material stage** rewrites the baked GLB before checks and sizing:
alpha mode and cutoff (`auto` measures transparent texels), culling, metallic replace and a linear roughness clamp
applied to the packed texture. A preservation proof accompanies the rewrite (checkpoint `material`; see
`docs/style-effects.md`). Each stage commits a checkpoint on the BuildRun; building the same approval again
after a failed attempt inherits its segment/sample checkpoints (no second TRELLIS.2 run). Raw/cut-out/mask stay on the build run
(`sources.intermediates` in the version record) and are not shipped as version files. **Re-export** (`:reexport`)
creates a new build run from the stored raw (also of a failed attempt) with changed exporter/texture/triangles/remesh
— no resampling. Triangle target precedence: re-export override > explicitly configured parameter > category budget
maximum > recipe default, clamped only to the exporter range; min/max stay advisory checks. Snapshots written by
a70232b (`model3d.default` v1 with other parameters) are refused with an explicit fork request.

Exporters: `clean` (default) is a port of o-voxel `to_glb` whose texture-space rasteriser is plain PyTorch; the image
contains no NVIDIA non-commercial code (a stub satisfies o_voxel's import). `research` uses upstream nvdiffrast v0.4.0
(research/evaluation-only licence), exists only in images built with `RESEARCH_EXPORTER=1`, and marks the version
licence `not_cleared`. On a real mesh the two agree on 99.46 % of texel→triangle assignments and to 6e-8 at p99 in
surface position (differences are edge tie-breaks); see `docs/acceptance.md`.

## Asset variants and families

Family membership lives only on `manifest.family_id` (`families/` records hold metadata + anchor, never a member list); the
index derives family search, filter and group-by-family pagination. One variant row = one one-item Job; the rows of one
plan share an immutable `VariantPlan` and a draft Batch.

Data flow: **draft** (`variant-drafts/<id>.json`, revisioned; source bound to one exact version + artifact sha256) → **references**
(source renders/2D prep via `:prepare-references`, guidance images) → optional draft-scoped aux planning
(`variant_analyze`, `variant_suggest`; suggestions never overwrite manual rows) → **plan** frozen at `:create-jobs`
(idempotent via command intent; family resolved/created) → **Jobs + draft Batch** → **edit generation** (`generate` in
edit mode: prompt enhancement in edit mode, then one source-conditioned edit per slot; never conditioned on sibling
candidates) → **QA compare** (`qa_compare`) → human approval (rounds; any set) → **build/sizing** (model3d: segment →
sample → bake → exact final sizing; 2D: kind build; direct rows: transform only, no prompt/candidates) →
**publication** as a new asset in the family with a `derivation` (source asset/version/artifact hashes, plan, row, method).

New stages: `variant_analyze`, `variant_suggest` (gpu1/aux, draft-scoped, invisible to Jobs/Batches), `qa_compare`
(aux `/compare`: resemblance, change, single-object, style, per-reference checks; coalesced), `diversity` (aux `/compare`
over pairs of the selected variants; digest-bound advisory report under `comparisons/variants/`).

Residency: an edit task's signature names the edit model (`qwen_image_edit_2511` + shared encoder/VAE) and sampler
settings but excludes source ids, row ids, prompts and seeds, so all variant Jobs share one GPU0 pass. Direct transforms
run on the cpu lane and never take a GPU grant.

ComfyUI: a **workflow registry** (`comfyui/workflows/*.bindings.yaml`, kinds `t2i`, `image_edit`) replaces the single
workflow; every binding lists model nodes and, for edit, the `conditioning` edges asserted before each submit (source
image must reach both text-encode nodes and the VAE-encoded latent). Sources are sent through a **controlled upload**
(content-named PNG, no overwrite, collision/non-PNG rejected) and referenced by handle. Edit builds omit speed/style LoRAs.

Aux v2 endpoints: `/enhance` (Conservative/Creative presets, edit mode, reference cues), `/compare` (strict yes/no; malformed
answers are `unsure`), `/analyze_source`, `/suggest_variants`. QA that cannot run is `unavailable`, never a pass.

`GET /events` (SSE) is now an async generator with non-blocking polling, so open browser tabs no longer hold threadpool workers.

## Security posture

Single trusted operator, loopback bindings. Mutations require the `X-AssetStudio: 1` header and same-origin `Origin`.
Uploads are inspected before storage (pixel caps, full decode, GLB container with no URIs). No user field is executable;
QA metrics are an allowlist; workflows are versioned files with node-id bindings validated against `/object_info`.

**MCP for remote agents** (`mcp_api/`, see `docs/mcp.md`). A second listener in the Studio process
(`STUDIO_MCP_HOST:STUDIO_MCP_PORT`, default `127.0.0.1:8191`) serves only Streamable HTTP `/mcp` and signed `/files/…`.
The UI/REST port stays loopback and unauthenticated, so only the MCP port may be exposed, behind a TLS proxy.
- Bearer tokens (`assetstudio mcp create|list|revoke`) are stored as sha256 in `instance/mcp_tokens.json` (0600). Scope
  is `read` or `full`. The store reloads on change, so revocation takes effect immediately.
- Tools call the REST API in-process (`httpx.ASGITransport`): validation, CSRF, idempotency, events and errors are shared
  with the UI.
- An agent may pass every gate (owner decision). Its identity reaches the REST layer as `x-assetstudio-actor`,
  accepted only together with a per-process secret. Commands persist the actor in their intent plan, and
  `ReviewDecision`/`WaveSelection` record `actor=agent:<token>`. New Jobs record `source=agent:<token>`.
- Bytes: inline base64 (≤ `STUDIO_MCP_MAX_INLINE_BYTES`, 16 MiB), or HMAC-signed URLs (per-process key, 15 min).
  An upload URL works once (exclusive spool file). Downloads stream the sha-verified artifact route. Studio never
  fetches agent-supplied URLs.
- DNS-rebinding protection admits loopback Host headers plus the host of `STUDIO_MCP_PUBLIC_URL`.
