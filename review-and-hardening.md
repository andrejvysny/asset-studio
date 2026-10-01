# AssetStudio — merged modular architecture review and hardening plan

**Reviewed snapshot:** `feat/modular-arch@11f5b18b5f58f11281cc5837d155ede91bf9b5f6`  
**Master snapshot:** `fffe1cdd1e5ee09ba939911db3ffd1a7fbbb034f`  
**Previous modular snapshot:** `e6a77bcc3a662f6256b91b753db6e70de5b845c1`  
**Review date:** 1 October 2026  
**Verdict:** changes requested before treating the combined system as production-ready. Preserve direct mode during hardening. Do not re-merge the already included master commit.

## 1. Scope and evidence

The feature head is a merge with the previous modular head as its first parent and current master as its second parent. The new master commit adds the Godot integration backend. This review distinguishes:

- **Newly included implementation:** Godot contracts, static-source publication, deliveries, integration credentials and change feed. Most issues in these modules were imported from master; they are not necessarily conflict-resolution mistakes.
- **Cross-system integration:** shared application lifetime, mutation draining, resource budgets, deployment and backup.
- **Retained modular weaknesses:** execution identity, slot concurrency, custody, scheduling and proxy-mode MCP authorization.

GitHub source and the parent-to-merge file comparison were inspected. No repository changes, pushes, deployments or production data mutations were performed.

Repository cloning was attempted but the container could not resolve `github.com`. Consequently, **the full repository tests, frontend build, Docker images, GPU acceptance and real proxy deployment were not executed in this review**.

Five isolated probes were executed in Python 3.13.5. Three exercise transcribed source excerpts with small test doubles; one evaluates the source capability expression; one models the nested lock topology with timeouts. They are **not end-to-end tests and not a substitute for the repository's pinned environment**. See `review_probes.py` and `probe-results.json`.

The repository reports lint clean, 959/960 backend tests on the merged head, the grouped-3D case passing on rerun, and 4/4 process tests. It explicitly says the merged head's GDScript, web and browser checks were not run. The queried GitHub Actions endpoint returned zero runs for this SHA. These are reported results, not independently reproduced results.

Sources: [feature head](https://github.com/andrejvysny/asset-studio/commit/11f5b18b5f58f11281cc5837d155ede91bf9b5f6), [master](https://github.com/andrejvysny/asset-studio/commit/fffe1cdd1e5ee09ba939911db3ffd1a7fbbb034f), [TODO](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/TODO.md).

## 2. Preserve these design choices

Keep the single canonical project store and existing publication primitive. The integration listener already publishes through them instead of inventing a second library. Keep exact asset/version references, content hashes, immutable descriptors/manifests and first-write semantics.

Keep the static source parser separate from the Godot runtime. Do not replace its allowlists, path checks and bounded extraction with loading arbitrary source packages in the main server. Preserve hostile fixtures and same-request crash-replay tests.

Keep the execution abstraction, runner-owned physical-device admission and temporary direct-mode compatibility. The architecture needs targeted completion, not another wholesale rewrite.

**Scope distinction:** a `GodotStaticSourcePackageV1` is not first-class Blender `.blend` support. Do not describe the new Godot backend as completion of the planned Mac/Blender authoring system.

## 3. Findings

Severity: **P1** means fix before production use of the affected feature; **P2** means important correctness, observability or maintainability hardening. A disabled public profile does not have to block an unrelated local-only experimental build, but it must not be advertised as validated.

### H01 — P1: independent credential writers can undo revocation

**Evidence:** reproduced with the token-store source excerpt and temporary files.

`IntegrationTokenStore` protects each instance with `threading.Lock`. The CLI can create separate store instances/processes. `_load()` followed by `_save()` is not a cross-process transaction. `write_private()` safely replaces a file but does not serialize its read–modify–write sequence.

A controlled interleaving reproduces the problem:

1. Writer A loads two live tokens and pauses before saving its revocation of alpha.
2. Writer B revokes beta and saves. A fresh reader rejects beta.
3. Writer A saves its old snapshot with alpha revoked and beta still live.
4. A fresh reader accepts beta again.

No real credential was used or printed. This is a local multi-writer race, not a claim of unauthenticated remote exploitation.

**Fix:** store integration credentials in a transactional instance database, or hold an OS-level lock over read, validation and atomic replacement. A lock on a file being replaced is not enough; use a separate stable lock file. Give each token an immutable credential ID. Bind preview ownership to that ID rather than the reusable display name: token creation currently permits reusing a revoked token's name.

Preserve current hashes/scopes during migration, fail closed on malformed storage, and never regenerate the server identity as error recovery. Review the MCP credential writer for the same class of race.

**Tests:** two independent processes concurrently revoke different tokens; create versus revoke; revoke/recreate the same display name; both successful revocations remain effective after restart.

Sources: [tokens.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/integration_api/tokens.py), [secure_files.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/secure_files.py).

### H02 — P1: crash recovery does not bind the full publication request

**Evidence:** source-confirmed; the extracted replay guard accepted unrelated changed fields.

Normal replay uses `journal.command_result(..., payload)` and checks the full request. After canonical publication succeeds but before the journal records its response, recovery instead finds the publication receipt and calls `_check_same_request()`.

That guard checks only `preview_id` and `package_sha256`. It does not compare portable hash, descriptor draft hash, target, expected pointer, name, tags, category, licence/credit fields or publishing identity. A changed request can therefore be accepted as a replay, and `_finalize()` can record that changed payload against the original publication.

The existing crash tests retry the original body. Their changed-name assertion comes **after** a successful replay has restored the full journal record, which does not cover the vulnerable interval.

**Fix:** durably bind a canonical digest of every semantic request field and stable principal before effects. Include that binding in the recoverable publication intent/receipt, not just the final journal response. Both recovery paths must use the same verifier. Recheck the operation under its serialization boundary before checking mutable current-version pointers, so concurrent identical requests do not spuriously become stale-pointer failures.

**Tests:** inject failure before `record_command`, then independently change each field and require `409 idempotency_conflict`. Replay the original body successfully. Include concurrent same-key calls, different publishers, and recovery after staging cleanup.

Sources: [source_publications.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/source_publications.py), [publication regression tests](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/tests/regression/test_integration_publication.py).

### H03 — P1: source deliveries omit required capabilities

**Evidence:** source-confirmed; the emitted capability expression was exercised locally.

The source validator detects `csg_static`, `static_collision` and `shader_source`. The preview stores `detected_capabilities` in the source validation summary. However, `_published()` builds source delivery capabilities as only:

```python
["godot_text_scene_v1"] + (["static_collision"] if parsed.collision else [])
```

`build_manifest()` writes that list as `required_capabilities`. CSG and shader-bearing packages therefore receive incomplete execution requirements. Collision may also be omitted when a source contains it but the optional descriptor collision claim is absent.

**Impact:** a client making compatibility/trust decisions from `required_capabilities` can select a delivery it should reject or gate. A warning elsewhere is not a replacement for the machine-readable requirement.

**Fix:** derive the manifest requirements from verified, representation-specific facts. Define handling of transitive dependency capabilities explicitly; clients must check each delivery in the closure. Do not infer required capabilities from optional publisher metadata alone.

**Migration:** changing immutable manifest bytes requires a new preparation/profile revision and delivery ID. Preserve old manifests and lockfile references; mark affected deliveries unsuitable for new resolution or provide an explicit replacement. Do not overwrite historical hashes in place.

**Tests:** publish `csg_hut`, `custom_shader_crystal` and the collision fixture; inspect the actual returned delivery manifests. Exercise a client lacking each required capability and a source with collision but no descriptor collision claim.

Sources: [source_scene.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/packages/assetstudio_processing/source_scene.py), [deliveries.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/deliveries.py), [delivery_projection.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/delivery_projection.py).

### H04 — P1 conditional risk: cross-library preparation has unsafe lock ordering

**Evidence:** source path inspected; nested lock topology reproduced, not the complete service deadlock.

`ensure_version()` holds `ctx.store.lock`. Source preparation calls `_add_dependency()`, which opens another library and recursively calls `ensure_version()`. Each `ProjectStore` owns a separate `RLock`.

With cold or incompletely prepared dependency deliveries, concurrent A→B and B→A preparation can wait in opposite lock order. The asset-version graph need not contain a cycle: A1 may depend on B0 while B1 depends on A0. Warm immutable dependency deliveries usually take the early return and avoid this interleaving; the risk is particularly relevant to restoration, migration and future cache rebuilding.

**Fix:** resolve and verify dependency closure without holding a project mutation lock. Then perform a short local compare-and-set commit of the prepared immutable record. Avoid recursive acquisition of project locks. If multi-project locking is unavoidable, impose one global order and test it.

Bound closure depth/size. Reject conflicting requirements for the same asset key rather than silently choosing one through `setdefault()` when transitive manifests disagree on representation or delivery.

**Tests:** simultaneous opposite-library cold preparation with a deterministic barrier; mixed cached/uncached dependencies; acyclic version graph; missing dependency; conflicting transitive delivery requirements. Test actual services, not just the lock microprobe.

Sources: [deliveries.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/deliveries.py), [ProjectStore](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/packages/assetstudio_storage/project.py).

### H05 — P1 availability: source failure can hide a valid portable GLB

**Evidence:** source-confirmed control flow, not a live outage reproduction.

`_expected()` requires both representations when an asset contains source and model. `_existing()` returns ready only if all expected deliveries exist. `_published()` can successfully prepare portable GLB and then return `temporarily_unavailable` because source dependencies cannot be resolved. `_resolve_one()` then returns the entire reference as unavailable, even when the request asked only for `portable_glb_v1`.

**Scenario:** a dependency library becomes unavailable between preview and delivery preparation, or source delivery preparation fails after the portable delivery was stored. The self-contained portable GLB should remain independently usable.

**Fix:** track readiness per representation; pass the requested representation set into preparation. Portable resolution must not require source closure. Return the available delivery and an explicit source-specific issue rather than discarding valid results.

**Tests:** request only portable while source dependency is unavailable; combined request with partial success; retry source preparation without changing the portable delivery ID/hash.

Sources: [deliveries.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/deliveries.py), [routes_assets.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/integration_api/routes_assets.py).

### H06 — P1 operational risk: per-request limits are not aggregate resource admission

**Evidence:** source-confirmed missing admission boundaries; no load or memory benchmark was run.

The integration API caps one upload at 512 MiB and source expansion at 1 GiB. Publication then reads the portable GLB fully, validates it, and may render a preview. Multipart spooling, staging copies, source expansion and in-memory processing have different lifetimes.

No integration-wide staging quota, shared disk-floor reservation, decoded-memory admission or dedicated processing limiter is visible in this path. Expired preview cleanup is opportunistic and deletes at most 50 directories per sweep. The canonical project lock also encloses `_register()` and its potentially expensive preview rendering.

REST, MCP and integration listeners run in one event loop/process. FastAPI sync endpoints and `run_in_threadpool` use AnyIO's shared default thread limiter. Heavy processing can therefore occupy capacity also needed by lightweight sync dependencies and file serving. Separate ports do not provide resource isolation.

**Fix:** introduce separate budgets for network staging, expanded bytes, processing memory and canonical storage. Reserve space before accepting work, including headroom for the journal. Use a bounded processing queue or explicit AnyIO limiter for heavy validation; move CPU-heavy previews out of the publication critical section. Periodic cleanup must understand active preview/commit ownership.

Retain a responsive control path and use typed retryable backpressure. Do not solve this by merely increasing the thread count. Preserve library-only operation when compute nodes are offline.

**Tests:** concurrent large previews with runner heartbeat traffic; repeated abandoned previews; full staging filesystem; corrupted oversized resources; bounded queue; stable control-request latency. Measure memory and disk peaks on target hardware before choosing final budgets.

Sources: [routes_publish.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/integration_api/routes_publish.py), [source_publications.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/source_publications.py), [capabilities](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/contracts/godot-integration/v1/capabilities.json), [Starlette thread-pool documentation](https://starlette.dev/threadpool/).

### H07 — P1: cutover and shared-listener shutdown need one authority boundary

**Evidence:** source-confirmed mode-switch race and uncoordinated shared-resource lifetime; not process-tested here.

The CLI sets the persisted execution mode and immediately clears `admission_paused`. The old process still runs its old backend and may claim queued work before restart. A periodically cached pause flag is not a transactional claim fence.

The integration commit path does not register work in the switch's live-work count. Separately, `main.run()` starts three servers with `asyncio.gather`, while the main FastAPI lifespan owns `st.close()`. No explicit barrier ensures companion requests and processing workers are drained before shared storage closes.

**Fix:** separate **compute admission pause** from **global maintenance/mutation drain**. Pure control-plane publication does not intrinsically require a GPU pause, but deployment, coherent backup and shared-store shutdown must track its in-flight writes.

Persist a switch generation: requested → draining → quiesced → activated. Fence the old process at claim/accept. Keep admission paused until the new process validates and activates the recorded generation. For shutdown, stop ingress, await tracked mutations and result custody, stop background maintenance/coordinator, then close shared state once.

**Tests:** queued work racing a switch, failed restart, lost CLI response, two switch requests, publication during maintenance, shutdown during hash/canonical commit, and companion listener startup failure.

Sources: [cli.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/cli.py), [main.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/main.py), [source_publications.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/source_publications.py).

### H08 — P2: change-feed overflow is ignored after the initial cursor check

**Evidence:** reproduced using the EventBus and collector excerpts.

The route checks for an expired cursor before entering its polling loop. `_collect()` subsequently does `batch, _ = bus.since(...)`, discarding the expiration signal. If the ring overflows during polling, it returns surviving events and advances the cursor with `reset_required=False`. Missing invalidations can leave the client's library stale.

**Fix:** return and propagate expiration on every collection, including the first collection after validation. Obtain the batch and its cursor-validity decision atomically from the bus. A gap must trigger a reset/full refresh.

Also harden listing pagination: `list_assets()` reads index revision separately from querying its page. Read revision and rows from one consistent snapshot, or detect intervening changes and return reset rather than associating rows with the wrong revision.

**Tests:** overflow after initial validation, overflow while no events are visible to the token, burst task events crowding out library events, restart epoch, and pagination under concurrent publication.

Sources: [routes_changes.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/integration_api/routes_changes.py), [events.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/events.py), [routes_assets.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/integration_api/routes_assets.py).

### H09 — P2: matching source declarations are not proof of scene correctness

**Evidence:** source-inspected gap; no full hostile-package fixture was executed in this environment.

Portable surfaces are checked against actual GLB mesh/primitive indices. Source surfaces are instead compared between the draft and manifest, then copied into the descriptor. `PackageChecker` validates types, references and reachability but does not establish an actual scene node/surface inventory against which those declarations are checked.

Similarly, descriptor collision metadata is checked against the presence of the capability, not the declared count/types. The reachability walk uses a visited set; that avoids looping in the validator but is not rejection of cyclic scene instancing or recursive shader includes.

**Fix:** record verified structural facts separately from publisher claims. Validate source node paths, applicable surface indices and collision shape claims where statically decidable. Reject unsupported structural claims or mark them unverified. Detect forbidden instancing/include cycles with bounded traversal. Do not claim to prove visual equivalence merely because two client-supplied conversion reports agree.

**Tests:** nonexistent node path, invalid surface index, wrong collision count/type, scene-instancing cycle, include cycle, mismatched report. Preserve valid shared acyclic dependencies; do not indiscriminately reject every reusable resource reference.

Sources: [source_publication_checks.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/source_publication_checks.py), [source_scene.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/packages/assetstudio_processing/source_scene.py), [source_manifest.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/packages/assetstudio_core/source_manifest.py).

### H10 — P1 for public deployment: Compose topology and routing are incomplete

`compose.public.yml` is documented as an overlay over both the base and node files. It does not remove inherited local GPU engines or the runner, so that command is not a Studio-only VPS deployment. It resets all Studio port mappings but defines a Traefik service only for port 8190. The MCP and integration listeners have no corresponding public route.

Keep this distinct from the intentional decision to disable the integration API by default. Once enabled in the public profile, it still needs explicit routing and its own token authentication. Do not route every path to 8190 or bypass its operator policy.

**Fix:** explicit Studio-only, compute-only and combined development configurations; separate TLS routes/services for operator REST/UI, runner API, MCP and integration API. Verify proxy header stripping and body/control budgets. Use a shared host-path lock for runner exclusivity across Compose project names, not a private project-scoped state volume.

Raise the documented minimum for `!override` to **Docker Compose 2.24.4**, not just 2.24. Render the effective configuration in CI and inspect mounts, ports, devices and inherited services.

Source: [compose.public.yml](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/compose.public.yml), [compose.nodes.yml](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/compose.nodes.yml), [Docker merge specification](https://docs.docker.com/reference/compose-file/merge/).

## 4. Retained modular issues — not resolved by this master merge

The parent-to-merge comparison leaves the runner lifecycle, remote aux/call adapters, attempt stores/services, placement and proxy operator-auth implementation unchanged. Do not mark the following closed merely because the new combined test count is larger.

| ID | Priority | Remaining issue | Required hardening |
|---|---|---|---|
| H11 | P1 for intended multi-GPU operation | `step → handle_offer → execute → spool → upload` is serial per runner. Heartbeat is separate, not a second execution lane. Acquire does not honor `free_slots`. | Independent slot supervisors; bounded transfer workers; real slot/device-instance mapping; available-slot filtering; no shared-device overlap. |
| H12 | P1 | Aux wire params drop execution IDs, while the executor broadly claims reconciliation support. Transport failure may be treated as terminal despite uncertain engine work. | Stable persisted effective engine IDs outside the logical digest; per-operation reconciliation contract; uncertainty retains ownership until explicit evidence. |
| H13 | P1 | Attempt commitment and disposition are separate writes; task completion and custody callbacks also have a crash gap. Late lost output cannot enter the normal upload states. | Transactional terminal disposition or durable finalization outbox; startup/periodic repair; authenticated custody-only quarantine path for old generations. |
| H14 | P1 | Runner quota check precedes reservation creation; recreated expired uploads can skip the quota check. | Atomic check/reservation; fresh reservation for expired retries; idempotent exact-field validation; coordinated finalize/cleanup and disk headroom. |
| H15 | P1 in proxy mode | In-process MCP REST calls lack proxy operator credentials and receive 401. | Trusted internal principal preserving read/full scope and actor, enforced by shared authorization; never a blanket localhost-owner bypass. |
| H16 | P2; P1 when selecting unsupported exporters | Geometry feature intersection blocks usable mixed fleets; exporter availability is not fully equivalent to per-call placement eligibility. Missing telemetry is reported as zero. | Typed per-slot features/exporters/model requirements; one preflight/placement/accept evaluator; unknown measurement = null with freshness, not idle. |

For H13, distinguish **durable custody transfer** from human acceptance. A verified canonical checkpoint may legitimately allow worker cleanup before the whole product workflow finishes. Never delay that until publication, but never issue a disposition without recoverable canonical ownership.

Sources: [agent.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/compute_node/assetstudio_node/agent.py), [engine_executor.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/compute_node/assetstudio_node/engine_executor.py), [remote/aux.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/remote/aux.py), [attempts.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/attempts.py), [transfers.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/transfers.py), [node_readiness.py](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/services/studio/assetstudio_server/services/node_readiness.py), [current TODO](https://github.com/andrejvysny/asset-studio/blob/11f5b18b5f58f11281cc5837d155ede91bf9b5f6/TODO.md).

## 5. Additional architecture hygiene

### H17 — P2: transport-specific types leak into application services

`source_publications.py` imports `IntegrationError` and `Principal` from `integration_api`. This makes the service depend on the listener's transport/auth implementation. Move the shared application principal and semantic service failures into an application-level module. Translate to HTTP/MCP at each adapter. Keep scopes distinct: a runner is not a publisher, and an integration publisher is not an operator owner.

Extend import tests to check direction inside the Studio package; existing package-level checks cannot catch this intra-package coupling. Keep this focused and avoid introducing a generic framework for every operation.

### H18 — P1 before cutover/restore: backup scope must preserve new identity

The current TODO explicitly identifies missing integration identity/token coverage. Back up canonical projects, a coherent journal, instance auth/signing state, MCP credentials and the integration directory including `server.json` and tokens. Document the runner-local state/spool recovery boundary separately.

A new server UUID changes asset reference identity. Accidentally generating one after restore is not harmless configuration drift. Take SQLite backups through an appropriate consistent backup/snapshot procedure, not a casual copy of a live database file. Rehearse restoration with published integration assets and uncertain attempts, not only an empty instance.

## 6. Implementation work packages

### WP0 — Establish a reproducible review baseline

Pin the reviewed head in a fresh worktree. Record exact environment, dependency lock, test counts and skips. Add failing regression tests before modifying H01/H02/H03/H08. Convert the isolated probes into repository tests using real services and process-level credential writers.

Treat the grouped-3D flake as unresolved evidence: capture task/attempt/pass transitions on failure, replace sleep-based synchronization where applicable, and repeat on baseline and candidate. A rerun pass alone does not explain the failure.

### WP1 — Credentials and authorization

Fix H01 and H15. Add stable credential identity, safe writer serialization, revocation tests and a shared trusted application principal. Do not change legacy full-token power accidentally; encode compatibility explicitly. Add negative tests for forged headers and separation of runner/integration/operator scopes.

### WP2 — Publication idempotency and custody

Fix H02 and the H13 durable finalization path. Persist full request/attempt binding before effects. Reconcile after process death without the original request remaining in memory. Preserve canonical hashes, current pointers and exact receipt identity. Do not combine this with a large concurrency rewrite.

### WP3 — Delivery correctness

Fix H03/H04/H05. Separate preparation from short commit locks, version affected profiles, preserve complete capabilities, detect conflicting dependency closure and return per-representation status. Decide the shape of partial resolve responses before downstream Godot clients implement a incompatible assumption.

### WP4 — Resource admission and source validation

Fix H06/H09/H14. Share disk accounting where storage is shared, but use separate execution/transfer budgets. Move render work out of project locks. Add explicit source structural evidence and unverified states. Keep the existing static safety checks.

### WP5 — Runner concurrency and recovery

Fix H11/H12/H16 after identity and custody are stable. Keep one control process per host and one authority per device set. Allow independent slots to overlap, including while previous results upload. Capture effective runtime/model/feature receipts with each attempt and reconcile before any re-execution.

### WP6 — Lifecycle, deployment and restore

Fix H07/H10/H18. Introduce persistent activation fencing and one shared application lifetime. Render deployment profiles in tests. Validate the public proxy routes on a real stack. Include integration credentials and server identity in a coherent restore rehearsal.

### WP7 — Invalidation and release evidence

Fix H08 and pagination snapshot consistency. Complete UI/error/freshness handling. Run full CPU, process, browser, GDScript, image-build and required hardware/proxy acceptance at one candidate SHA. Record evidence without counting skipped mandatory scenarios as successful acceptance.

Suggested commit units: one failing test plus one fix per bounded concern. WP1 and WP3 can proceed in parallel if ownership of principal, protocol and delivery-profile changes is agreed. Avoid multiple agents independently changing shared state machines or the same contract files.

## 7. Regression acceptance matrix

| Test | Scenario | Required invariant |
|---|---|---|
| AT01 | Two processes revoke different credentials | Neither revocation is undone. |
| AT02 | Create/revoke and display-name reuse | Credential identity, not name, owns old previews. |
| AT03 | Crash before publication journal response; change each semantic field | Changed request conflicts; exact replay converges. |
| AT04 | Two identical commit requests concurrently | One version; no spurious stale-pointer failure. |
| AT05 | CSG, shader and collision source fixtures | Manifest requirements contain all verified capabilities. |
| AT06 | Corrected preparation profile | New delivery identity; old immutable bytes unchanged. |
| AT07 | Opposite-library cold dependency preparation | No nested-lock deadlock; bounded completion/error. |
| AT08 | Conflicting transitive deliveries for one asset key | Explicit conflict; no silent first-wins selection. |
| AT09 | Portable request while source dependency unavailable | Portable remains usable; source has independent failure. |
| AT10 | Concurrent large previews plus heartbeats | Memory/disk bounded; control traffic remains responsive. |
| AT11 | Abandoned previews and staging disk full | Quotas enforce reserve; journal remains writable. |
| AT12 | Source node/surface/collision mismatch | Rejected or explicitly unverified, never falsely verified. |
| AT13 | Instancing/include cycles and valid shared dependencies | Forbidden cycles rejected; valid reuse preserved. |
| AT14 | Overflow during active long poll | Reset required; no silent gap. |
| AT15 | Page query racing publication/index update | Consistent revision/page or explicit reset. |
| AT16 | Independent runner slots, slow upload | Real execution overlap on independent devices. |
| AT17 | Two slots share physical GPU | No simultaneous heavy work. |
| AT18 | Aux response lost after engine admission | Same engine identity reconciles or remains uncertain. |
| AT19 | Process dies between terminal state and disposition | Repair produces exactly one durable disposition. |
| AT20 | Lost-generation output arrives late | Quarantined custody; never promoted to current. |
| AT21 | Concurrent quota reservations and expired upload retry | Reservations remain within limits and are counted once. |
| AT22 | Proxy-mode MCP read/full/revoked/forged principal | Correct authorization and actor attribution. |
| AT23 | Mixed exporter/feature/model fleet | Correct slot selected or precise unsatisfied requirement. |
| AT24 | Mode switch with queued/in-flight work | Old generation cannot admit work after handoff. |
| AT25 | Integration commit during shutdown/maintenance | Shared state not closed while mutation still owns it. |
| AT26 | Listener startup failure / interrupted activation | No partly active uncontrolled deployment. |
| AT27 | Rendered Studio-only/compute-only/public configs | Only intended services, routes, mounts and devices. |
| AT28 | Real proxy routes and forged headers | Each listener authenticates correctly; no backend bypass. |
| AT29 | Two Compose projects on one GPU host | Shared host lock prevents duplicate runner authority. |
| AT30 | Restore with published integration assets and live/uncertain attempts | UUID/hash continuity; documented custody recovery. |
| AT31 | Newest master feature regressions | Profiles, material rebuilds, style/reference binding, MCP and publication gates retained. |
| AT32 | Two-runner GPU chaos | Kill the process bound to the observed attempt, not a hard-coded container name. |
| AT33 | Runtime measurement unavailable | Unknown/null, never fabricated zero utilization. |
| AT34 | Full merged-head frontend/GDScript/contracts validation | No stale generated output; no unacknowledged mandatory skip. |

## 8. Test commands and evidence collection

These commands are proposed for the repository owner/coding agent in a fresh checkout. They were **not executed against the full repository here**. Use the repository's supported Python/dependency environment and preserve production data.

```sh
uv sync --frozen
make lint
uv run pytest -q -ra
make test-process
make web-build
make e2e
uv run python scripts/make_integration_fixtures.py --check
uv run pytest tests/unit/test_integration_openapi.py tests/unit/test_openapi_fresh.py -q -ra
uv run pytest tests/unit/test_godot_canonical.py -v -ra
```

The GDScript check requires the supported Godot executable; a skip does not certify canonical cross-language agreement. Run worker-image tests and build the actual deployment images as separate gates. Review generated OpenAPI/TypeScript/fixture diffs before accepting regenerated output.

On the configured GPU acceptance host, after selecting the correct test deployment:

```sh
make acceptance-gpu
GPU_NODES_DOCKER=1 GPU_NODES_CHAOS=1 GPU_NODES_RUNNER_B=1 make acceptance-gpu-nodes
```

Those flags require the corresponding hardware and topology. The acceptance runner must fail when a release-required scenario is skipped. Add same-runner independent-slot overlap: two-runner tests alone do not demonstrate it.

Store commit SHA, lockfile checksum, image digests, rendered Compose config, command, environment, passed/failed/skipped counts and hardware details with every run. Never include plaintext credentials in evidence artifacts.

## 9. Release gates

1. The affected P1 issues are fixed or the feature is explicitly disabled and excluded from the release claim.
2. New regression tests fail on the reviewed code and pass on the candidate.
3. Full request replay, revocation, immutable capabilities, custody and activation fences survive process failures.
4. Full merged-head CPU/browser/GDScript checks pass with explained skips; the grouped-3D flake is characterized and fixed or explicitly constrained.
5. Target node hardware, proxy deployment and coherent restore have recorded evidence.
6. Direct mode remains available until node-mode acceptance and cutover are approved. Its later removal is a separate change, not part of incident recovery.

Do not add Blender authoring, new model families or a broader product redesign to this hardening series. Stabilize the now-shared execution, publication and delivery contracts first.
