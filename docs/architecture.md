# Architecture and data ownership

## Components

```
browser ──HTTP/SSE──> studio (FastAPI) ── coordinator lanes ──> comfyui (GPU0)   stock nodes, versioned graph
                         │                                   └─> aux (GPU1)      enhance / VLM QA / BiRefNet
                         ├── ProjectStore ──> project root (portable: YAML + JSON + blobs/sha256)
                         ├── AssetIndex (SQLite, instance dir, rebuildable)
                         └── Journal (SQLite, instance dir, live operations + command idempotency)
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

## Source of truth

| Data | Where |
|---|---|
| project configuration | `studio.yaml` (revisioned; every edit names its expected revision) |
| effective config per batch item | `config/revisions/<sha256>.json` (immutable snapshot) |
| requested assets | `shotlist.yaml` (status derived from batches + publications) |
| assets and versions | `manifests/<asset>.json` (current pointer, conditional update) + `versions/<asset>/<ver>.json` (immutable) |
| bytes | `blobs/sha256/ab/cd/<sha256>` (immutable, deduplicated) + `artifacts/<id>.json` (role, lineage, provenance) |
| batch history | `batches/<id>/{batch.json,items,prompts,candidates,qa,decisions,builds}` |
| live dispatch | instance journal (SQLite, same host only) |
| search | instance index (SQLite, rebuilt from manifests: `storage:rebuild-index`) |

## Lifecycle

Per item, never batch-wide: brief → enhanced prompt revision → **human confirms exact revision** → candidate set
(+ advisory QA) → **human approves exact candidate** (set id, candidate id, sha256 re-hashed from stored bytes, prompt
revision, QA evaluation, item revision) → build run → **human accepts final result** (structural validation mandatory)
→ publication (per asset, idempotent, derived ids). Regeneration creates a new prompt revision and candidate set for
selected rows only; history is never overwritten.

## Scheduling and recovery

- One coordinator thread per lane (`gpu0`, `gpu1`, `cpu`); affinity-preferring claims (bounded streak).
- Operations are journaled before dispatch (`held` → items marked → `queued`), so a claim never races item state.
- ComfyUI prompts use deterministic UUIDs (`uuid5(op, item, idx)`): after a lost response or restart the handler looks
  the prompt up in `/history` and `/queue` before any resubmission. Cancel = dequeue by id + targeted `/interrupt`.
- On restart, `running` operations become `reconciling` and resume idempotently; blocked retryable work is retried.
- GPU1 ownership: every worker not verified-released must acknowledge `/unload` with `{loaded: false, owner_token}`.
  Timeouts, resets, HTTP errors, malformed bodies or `loaded: true` leave ownership **unknown** and block dispatch
  until an operator runs the lane reset (Runtime screen).

## Security posture

Single trusted operator, loopback bindings. Mutations require the `X-AssetStudio: 1` header and same-origin `Origin`.
Uploads are inspected before storage (pixel caps, full decode, GLB container with no URIs). No user field is executable;
QA metrics are an allowlist; workflows are versioned files with node-id bindings validated against `/object_info`.
