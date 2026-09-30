# Compute runner specification v1

Status: normative for Phases 1–2 of the modular-system refactor. Refines `Modular_system_spec.html` §4, §11–§14 and
§17 for GPU execution. Where this document and the report differ on runners, this document wins. Everything here is
a target design; `docs/modular/migration.md` tracks what is implemented.

A **runner** is the compute-node agent of the report, modelled on GitHub Actions runners: it needs no public IP,
connects outward to Studio, and executes only the work Studio assigns to it. Studio stays the only product authority
(I01): runners never approve, publish, read project records or choose target paths.

## R0 Fleet envelope

Defaults and tests are sized for at most 3 runners, 6 GPUs and one Studio host (a public VPS in profile P). Every
limit below is configuration with a default and a test; P7 measures real values.

## R1 Terms

| Term | Meaning |
|---|---|
| Runner | One agent process per machine (enforced by a host lock, R7). |
| Slot | An exclusive group of physical devices on one runner serving one capability class: `image` (ComfyUI) or `aux3d` (aux + worker3d). |
| Operation call | One engine invocation: `image.t2i`, `image.edit`, `aux.enhance`, `aux.compare`, `aux.qa`, `aux.cutout`, `aux.analyze_source`, `aux.suggest_variants`, `worker3d.generate`, `worker3d.export`. |
| Attempt | One execution of one operation call on one runner session. Identity `(task_id, call_key, generation)`. |
| Continuation | The Studio stage function (`coordinator/stages/*`) that issues calls. It runs in Studio, never on a runner. |

## R2 Deployment profiles

The protocol and authentication are identical in every profile.

| Profile | Topology | Dispatch | Notes |
|---|---|---|---|
| **S** single machine (first target) | Studio, runner and engines on one host (compose) | pull over the internal network (default) or direct push | Studio has no `/models` mount; the runner owns the GPUs |
| **P** public Studio, NAT runners | Studio on a VPS behind Traefik + Authelia; runners at home | pull only (outbound 443; `HTTPS_PROXY` honoured) | direct push needs an inbound path, which NAT runners do not have |
| **R** reachable runner (optional) | runner on LAN, VPN or public IP | direct push allowed | push URL registered by the operator only |
| **E** ephemeral | autoscaled / cloud GPU from a pre-provisioned image | pull, at most one attempt, then deregister | no model downloads during an attempt |

## R3 Identity and authentication

- An operator with role `owner` creates a **registration token**: single use, TTL ≤ 1 h, scoped to a *runner group*
  (allowed projects, allowed operations, operator labels, `ephemeral` flag). Only operator-assigned labels feed
  authorization decisions; runner-declared facts are capabilities, not permissions.
- The runner generates an **Ed25519 keypair** locally (private key file mode 0600, never transmitted) and calls
  `register` with the public key and the registration token. Registration is idempotent by public key: a retry with
  the same key and token returns the same `runner_id`.
- **Access tokens**: `GET /token/challenge` returns a nonce (TTL 60 s, single use, atomically consumed, bound to the
  runner and audience). The runner signs `{runner_id, nonce, aud}` and receives an opaque access token (TTL ≤ 15 min,
  stored as sha256, scoped to the runner). No clock synchronisation is required.
- **Revocation** refuses new challenges and invalidates live tokens immediately; it is re-checked at accept,
  finalize and report.
- **Transfer grants** are per attempt, per sha256, expiring and purpose-bound (download input X, upload a result of
  call Y).
- **Direct push**: Studio signs offers with its own Ed25519 key. Runners pin a key set (current + next) delivered at
  session start and reject unsigned or stale offers. Key rotation publishes the next key before switching.
- Single-machine bootstrap: `assetstudio runners token --group local` produces a token passed to the runner as a
  compose secret.
- Credentials live in `instance/auth.sqlite`, never inside project backups. `auth.sqlite` and the Studio signing key
  have their own instance backup/restore procedure. Restoring a journal without them forces runners to re-register;
  their local attempts then reconcile by `(task_id, call_key, generation)` and unknown ones stay `uncertain`.

## R4 Sessions and versioning

`POST /sessions` with `{runner_id, boot_id, protocol_versions, software, platform, dispatch, local_attempts[]}` returns
`{session_id, protocol_version, catalog, catalog_sha}`. Session creation is idempotent by `boot_id`. A new session
supersedes the previous one; calls carrying a superseded `session_id` fail with `stale_session`. `local_attempts`
(id, generation, state, spooled result hashes) drive reconciliation on every reconnect. An incompatible protocol or
catalog fails with `protocol_incompatible` and an actionable explanation (A38).

## R5 Dispatch

Delivery differs between pull and push; the commit point is identical.

- **Offer**: Studio places a call on an eligible slot. The attempt becomes `offered` with its generation, an offer
  deadline, frozen inputs, requirements, policy and `input_digest`.
- **Pull**: `POST /sessions/{sid}/acquire {request_id, free_slots, cached_residencies, wait_s ≤ 50}` returns an offer
  or 204. It is an async long-poll with `wait_s` below the proxy idle timeout. Repeating a `request_id` returns the
  same offer (lost response); `request_id`s are retained for the offer TTL. One outstanding acquire per session.
- **Direct push**: Studio `POST {registered_url}/v1/offers` with a signed body. The URL is registered by the operator,
  never declared by the runner (no SSRF); redirects are not followed. Only loopback/internal networks or TLS with the
  runner's pinned key are allowed. The runner still reports, downloads and uploads through the runner API.
- **Accept** `POST /attempts/{id}/accept {generation, session_id}` is the only commit point: a conditional
  `offered → leased` transition that atomically validates the continuation (task running, control `run`), the
  generation and the device reservation. A runner never starts work before accept returns 200.
- **Accept replay**: a repeated accept from the same session and generation returns 200 with `start_authorized` =
  lease still valid ∧ control `run` ∧ runner not revoked. Expired or superseded ⇒ 409 `stale_generation`; cancelled ⇒
  409 `cancelled_by_operator`. The runner persists the attempt in its local database **before** invoking an engine
  and deduplicates by attempt id.
- **Reject** carries a reason (admission busy, missing model, resource) and triggers re-placement. Repeated rejects
  appear in the task's "why not running" explanation. Expired offers answer 410 so the runner re-acquires.

## R6 Leases, heartbeats, uncertainty

- The heartbeat (default 15 s) carries active attempt states and renews leases. Its response carries control intents
  (cancel) for pull runners; direct-push runners also receive pushed cancels.
- A missed lease makes the attempt `uncertain`. Its slot and physical devices stay **reserved**: unknown ownership
  blocks those devices only (I06); other slots continue.
- Resolution: the runner reconnects and reports, or an operator declares the attempt `lost`. The next generation may
  then run **elsewhere**, but the original device claim is kept until drain or termination evidence (R7 barrier) or
  until that device is retired permanently.
- Ephemeral runners: the logical attempt may be auto-retired after TTL + revocation. The device claim is released
  only when the provider reports the instance terminated or the device identity is retired forever.

## R7 GPU authority

- Device identity is the NVIDIA **physical UUID** (global). The fallback `runner_id/index` is allowed only when the
  UUID is unavailable, and then only under the host lock.
- One agent per host: an exclusive lock file on a host-path volume that every runner container on that host mounts.
  A runner rejects slot maps with overlapping device groups; Studio refuses a second runner advertising a UUID
  already claimed (A02, A03).
- Local admission is the existing `GpuLane` (monotonic epochs, acknowledged unload) per slot. Several engines may
  share one device only when every one of them acknowledges unload; ComfyUI never shares a device in v1.
- **Engine recovery barrier.** The host lock proves only that one *agent* runs; engines can outlive it. Before a slot
  is advertised after any agent start: reconcile known executions by id (worker3d status, ComfyUI prompt lookup);
  every leased worker, **including the one about to be granted**, acknowledges unload at a new persisted epoch with
  `active = 0`; unknown ComfyUI queue entries block the slot until drained or reset by an operator. Epochs are
  persisted in the runner database. In node deployments ComfyUI has no host port, so only the runner submits work.

## R8 Execution, identity and receipts

### Continuation versus placement

The Studio task is the continuation. A Studio worker thread owns it through `taskstore.claim` (a short transaction,
never held across a remote wait). Each remote call is placed **independently**: offer → accept binds only that
attempt to a slot and device reservation. A pass keeps a *preferred slot* (residency affinity) as a soft hint; when
that slot is lost, busy, or an ephemeral runner has used its one attempt, the next call is placed elsewhere. A task
whose call is `uncertain` suspends (blocked, retried by the existing retry loop) without holding a thread or a
reservation on other devices. Resumption replays completed calls from their ingested attempts instead of recomputing.

### Operation identity table

`call_key` is derived from logical inputs only; retries of the same logical call reuse it. Generation 1 keeps every
legacy id that is already stable. Generation ≥ 2 derives new engine execution ids and output artifact ids, because
`ProjectStore.register_artifact` refuses a known id with different bytes (`packages/assetstudio_storage/project.py:169`).
A call re-issued under an existing `call_key` with a different `input_digest` fails with `stale_revision`.

| Stage / site | Operation | Legacy call id | `call_key` (node mode) | Output artifact ids | Change needed |
|---|---|---|---|---|---|
| `generate` `stages/generate.py:248` | `image.t2i` / `image.edit` | `engine_prompt_id(task, slot)` | `task/slot` | `derived_id("art", task, slot)` (`:217`), candidate `derived_id("cnd", set, slot)` | gen ≥ 2 suffixes prompt and artifact ids |
| `enhance` `stages/prompt.py:69` | `aux.enhance` | `derived_id("att", task, attempts)` | `task/enhance` | none (prompt revision record) | **unstable**: legacy id includes `t.attempts` |
| `mask` `stages/qa.py:64` | `aux.cutout` | `derived_id("att", task, candidate)` | `task/candidate` | `derived_id("art", task, candidate)` (`:70`) | gen ≥ 2 suffix |
| `qa_vlm` `stages/qa.py:80` | `aux.qa` | `derived_id("att", task, candidate)` | `task/candidate` | none (QA record) | — |
| `qa_compare` `stages/qa_compare.py:120` | `aux.compare` | `derived_id("att", task, tag)` | `task/tag` | none | — |
| `diversity` `stages/diversity.py:34` | `aux.compare` | `derived_id("att", task, i, j)` | `task/i/j` | none (report) | — |
| `variant_analyze` `stages/variant_plan.py:57` | `aux.analyze_source` | `derived_id("att", task, attempts)` | `task/analyze` | none | **unstable** (attempts) |
| `variant_suggest` `stages/variant_plan.py:87` | `aux.suggest_variants` | `derived_id("att", task, attempts)` | `task/suggest` | none | **unstable** (attempts) |
| `segment` `builds/common.py:123` | `aux.cutout` | none | `run/segment` | `derived_id("art", run, "mask")` | **no id today** |
| `sample` `builds/model3d.py:125` | `worker3d.generate` | `{run}-sample-1` (persisted on the BuildRun) | `run/sample` | `derived_id("art", run, "raw", eid)` | already per execution |
| `bake` `builds/model3d.py:154` | `worker3d.export` | `{run}-bake-1` | `run/bake` | `derived_id("art", run, "model", eid)` | already per execution |

Stages receive a small `CallContext` (`call_key`, `generation`, receipt) and derive output ids through
`env.output_id(role, …)`. Direct mode returns today's ids unchanged, so existing records and tests are unaffected.

### Attempt states

`offered → leased → admitted → executing → spooled → uploading → ingested → committed`, or terminal
`failed | lost | cancelled | uncertain | quarantined`. Control intent (`run` / `cancel`) is a separate field.

1. The runner executes and writes outputs plus a result manifest to its spool (fsync, sha256). Only then does it
   acknowledge worker3d, so the worker never deletes its only durable copy early (I07).
2. Resumable upload (R9). Studio verifies the full sha256, writes the project CAS blob and records the manifest on the
   attempt in the journal: `ingested`. This state is **replayable**: re-running the stage re-reads the attempt and
   never recomputes.
3. The stage completes; task result and downstream tasks commit together (existing `TaskStore.complete`); the attempt
   becomes `committed`.
4. A **disposition receipt** is returned on the next heartbeat or report; only then may the runner delete its spool.
   After a lost receipt the runner asks `GET /attempts/{id}/receipt` before deleting.

Every terminal attempt gets exactly one disposition, so spools always drain:

| Disposition | When | Bytes |
|---|---|---|
| `committed` | task succeeded | referenced by the task result |
| `rejected` | output failed validation | retained by Studio as evidence |
| `cancelled` | task cancelled after ingest | retained by Studio until retention GC |
| `quarantined` | late result of a superseded generation (A11) | retained, never current |

Runner lifecycle flags: `draining` (no new assignments) → `custody_transferred` (every output has a receipt) →
`safe_to_terminate` (ephemeral runners deregister). Offline spool retention defaults to 7 days, then alerts; spools
are never deleted silently.

Node-mode startup: running tasks with non-terminal attempts go to `reconciling` and are never placed on another
runner; a pending cancel stays pending until the runner acknowledges it. This replaces
`TaskStore.recover_after_restart` behaviour in node mode only.

### Ownership lifetimes

| Owner | Starts | Ends |
|---|---|---|
| Continuation (task) | `taskstore.claim` | task terminal or suspended |
| Attempt lease | accept 200 | disposition receipt, `lost` declaration or expiry → `uncertain` |
| Device claim | first accept on that device | R7 barrier evidence or permanent retirement (never on lease expiry) |
| Spool custody | runner writes the spool | disposition receipt |

## R9 Transfer

- Inputs: `GET /attempts/{id}/inputs/{sha}` with `Range` support. The runner keeps a CAS cache and verifies bytes on
  use. Cache presence is a scheduling hint, not proof of integrity.
- Results: `POST /uploads {attempt, generation, sha256, size, role}` → `upload_id, chunk_size`, after taking a
  **storage reservation** for the declared size; `PUT /uploads/{id}/chunks/{n}` with a per-chunk sha256 and exact
  `Content-Length`; `GET /uploads/{id}` → received ranges; `POST /uploads/{id}/finalize` → full-file verification.
- Chunks are immutable and idempotent: the same index with the same sha returns 200, a different sha returns 409.
  The upload is frozen during finalize. Abandoned uploads are garbage-collected after a TTL. Handlers stream and
  never hold a whole file in memory.

## R10 Readiness and model assurance

Studio sends the catalog (the content and sha256 of `config/models.lock.yaml`) at session start. Every catalog file
carries a pinned sha256; the runner full-hash verifies each file before first admission and then relies on the
identity cache (`HashCache`). A file without a pinned hash is not schedulable. Readiness is computed per operation
and per recipe chain across operations, from fresh inventories of authorized slots. A model revision is never
substituted silently (A04, A05).

## R11 Public exposure (profile P)

- Traefik routes `/api/runner/v1/*` around forward-auth (runner authentication only); every other path requires
  Authelia forward-auth.
- Studio listens only on the proxy network. Identity headers (`Remote-User`, `Remote-Groups`) are trusted only
  together with a proxy shared-secret header; the proxy strips client-supplied identity headers. The CSRF header and
  Origin check stay in place.
- Per-runner rate limits and body caps; one long-poll per session; an audit log of registrations, token failures,
  revocations, offers, accepts, uploads and quarantines. Studio never fetches a runner-supplied URL.

## R12 Errors

Stable machine codes with readable messages: `invalid_input`, `unsupported_contract`, `protocol_incompatible`,
`unauthorized`, `forbidden_scope`, `stale_session`, `stale_generation`, `stale_revision`, `node_unavailable`,
`uncertain_execution`, `missing_artifact`, `validation_failed`, `resource_exhausted`, `admission_rejected`,
`cancelled_by_operator`. Remote adapters map them onto the existing adapter exceptions so
`coordinator/errors.py:classify` keeps its scope rules: an unreachable runner blocks the resource, an invalid output
fails only its item.

## R13 Observability

The runner view shows online/stale/offline, profile, dispatch mode, devices (UUID), slots, verified and loaded
models, active attempts, unsynced spool bytes, upload progress, recent errors, and per task the reason it cannot run
(no compatible model revision, insufficient resources, disconnected runner, unknown device ownership).

## R14 Resource budgets on a public endpoint

- Unauthenticated endpoints (`register`, `token/challenge`, `token`): per-IP rate limits in Traefik and an in-app
  token bucket per (endpoint, IP) (`STUDIO_RATELIMIT_PER_MIN`=10, `STUDIO_RATELIMIT_BURST`=5; 429 `resource_exhausted`
  with `Retry-After`; LRU of 10k buckets); outstanding nonces are capped; `register_refused`/`token_refused` audit rows
  are aggregated to one row per (event, IP) per minute with a `count`. The client IP is the socket peer, or the first
  `X-Forwarded-For` hop only when the proxy secret header is valid.
- Storage: global, project and runner byte quotas; reservation before upload; a **disk reserve** below which uploads
  are refused so journal and control writes always succeed. Rejected and quarantined bytes count against quota.
- CPU and memory: semaphores bound concurrent finalize/hash work and in-flight remote-result bytes (remote adapters
  still return bytes to stage code). An oversize result blocks its task with a reason instead of exhausting memory.
- Control-plane isolation: heartbeat, acquire and accept are async and never queue behind transfers. Transfers use a
  separate Traefik router with in-flight limits and a bounded threadpool in Studio.

## R15 Execution-mode fence

The journal records `execution_mode` and an activation marker. Startup refuses when the configured mode differs from
the persisted one while non-terminal tasks or attempts exist. Switching is explicit:
`assetstudio execution switch --to nodes|direct` pauses admission, waits until no task is running or reconciling and
no attempt is non-terminal, then flips (calls of tasks already running may finish; nothing new starts). A configured
mode that differs from the persisted one is recorded automatically only when nothing is in flight, so nothing can be
orphaned; otherwise startup is refused. The journal refuses to open a schema version newer than it knows.

## R16 Operator roles

| Role | Allowed |
|---|---|
| `viewer` | read-only (GET) |
| `reviewer` | viewer + prompt confirmation, candidate approval, build acceptance, publication, wave actions |
| `owner` | everything, including project configuration, runner groups and registration tokens, execution switch, GPU lane reset |

A route-table dependency (method + path group) enforces roles and fails closed for unmapped mutating routes. In
profile S (loopback, no proxy) the local operator is `owner`.

Identity: `STUDIO_AUTH_MODE=local|proxy` (default `local`). In `proxy` mode Studio refuses to start without
`STUDIO_PROXY_SECRET` (or `_FILE`); every `/api/` request except `/api/runner/*` and `/api/health` must carry
`X-AssetStudio-Proxy-Secret` (constant-time compare, else 401), `Remote-User` (else 401) and a `Remote-Groups` entry
mapped by `STUDIO_ROLE_GROUPS` (default `owner:assetstudio-owners,reviewer:assetstudio-reviewers,
viewer:assetstudio-viewers`; highest mapped role wins, none = 403). The audit actor is the `Remote-User` value
(`operator` in local mode). The table lives in `operator_auth.ROLE_RULES`; `tests/unit/test_operator_auth.py` fails
when a mutating route matches no rule.

### Appendix: route to role

GET/HEAD/OPTIONS are `viewer`, except `GET /api/v1/audit` (`owner`). Mutating routes (`{p}` = `/api/v1|v2/projects/{id}`):

| Role | Routes |
|---|---|
| `reviewer` | `POST {p}/(batches\|jobs\|runs)/{id}:{edit-prompts, confirm-and-generate, confirm-prompts, mark-regenerate, regenerate, approve-candidates, clear-approval, preview-best, build-approved, accept-builds, publish}`; `POST {p}/runs/{id}:{pause, resume, cancel, close}`; `POST {p}/variant-plans/{id}:compare-selection` |
| `owner` | project create/register, `config`, `config:validate`, `storage:test`, `storage:rebuild-index`; `POST /api/v1/runtime/lanes/{lane}:reset`; operation and task `:cancel`/`:retry`; families, assets and `:set-current`; imports, shot list, references and media upload/edit/archive/restore; job and batch create, update, `:plan`, `:start`, `:run`, `:cancel`, `:enhance`, `:reexport`, `:run-transform`, `:retry-preview`, item reference and preset edits; variant drafts (all actions); `POST /api/v1/runner-groups`, `.../registration-tokens`, `/runners/{id}:revoke`, `PUT /runners/{id}/push-url`, `/attempts/{id}:declare-lost` |
| unmapped | any other mutating route: `owner` (fail closed) |

Execution switch is a CLI command (`assetstudio execution switch`), not an HTTP route. Task and operation cancel/retry
are `owner` for now (conservative); promote them to `reviewer` if reviewers need to unstick their own waves.

## R17 HTTP surface (`/api/runner/v1`)

Bodies are the `assetstudio_protocol` DTOs. Errors are `ErrorBody` JSON with the HTTP status below. Every route except
the first three requires `Authorization: Bearer <access token>`; routes taking a session also check that the session
is the runner's active one (`stale_session`, 409).

| Method + path | Body → response | Notes |
|---|---|---|
| `POST /register` | `RegisterRequest` → `RegisterResponse` | unauthenticated; 403 `unauthorized` with reason on refusal |
| `POST /token/challenge` | `ChallengeRequest` → `Challenge` | unauthenticated; rate-limited (R14) |
| `POST /token` | `TokenRequest` → `AccessToken` | 401 on a bad signature, consumed/expired nonce or revoked runner |
| `POST /sessions` | `SessionHello` → `SessionAccepted` | idempotent by `boot_id`; 409 `protocol_incompatible` |
| `PUT /sessions/{sid}/inventory` | `Inventory` → `InventoryAck` | 409 `forbidden_scope` on a device already claimed by another runner |
| `POST /sessions/{sid}/heartbeat` | `Heartbeat` → `HeartbeatResponse` | renews leases, returns controls and receipts |
| `POST /sessions/{sid}/acquire` | `AcquireRequest` → `Offer` or 204 | long-poll up to `wait_s`; same `request_id` ⇒ same offer |
| `POST /attempts/{id}/accept` | `AcceptRequest` → `AcceptResponse` | commit point (R5); 409 `stale_generation` / `cancelled_by_operator` |
| `POST /attempts/{id}/reject` | `RejectRequest` → 204 | re-placement |
| `POST /attempts/{id}/report` | `ReportRequest` → `ReportAck` | progress/state; late stale generation ⇒ 409 `stale_generation` |
| `POST /attempts/{id}/complete` | `CompleteRequest` → `ReportAck` | result manifest referencing finalized uploads |
| `GET /attempts/{id}/receipt` | → `DispositionReceipt` or 404 | lost-receipt recovery |
| `GET /attempts/{id}/inputs/{sha256}` | → bytes | `Range` supported; only hashes bound to the attempt |
| `POST /uploads` | `UploadCreate` → `UploadCreated` | storage reservation; 507 `resource_exhausted` |
| `PUT /uploads/{uid}/chunks/{n}` | bytes (`X-Chunk-Sha256`, exact `Content-Length`) → `ChunkAck` | 409 on a conflicting chunk |
| `GET /uploads/{uid}` | → `UploadStatus` | resume |
| `POST /uploads/{uid}/finalize` | → `IngestReceipt` | full-file verification |
| `DELETE /runners/self` | → 204 | ephemeral deregistration after `safe_to_terminate` |

Small DTOs added for this surface: `ChallengeRequest{runner_id}`, `AcquireRequest{request_id (uuid4), free_slots,
cached_residencies, wait_s 0..50}`, `ReportRequest{session_id, report: AttemptReport}`, `CompleteRequest{session_id,
manifest: ResultManifest}`, `InventoryAck{accepted, revision}`, `ReportAck{state}`, `ChunkAck{status:
stored|duplicate}`.
