# Modular-system migration

Tracks the move from in-process execution to Studio + compute runners (Phases 0–2 of the modular-system plan),
then the Blender source lifecycle (P3+). Normative runner behaviour: `docs/modular/compute-runner.md`. Target
architecture: `Modular_system_spec.html` (repository root). Implementation progress: `TODO.md` ("Modular system").

## Rules

- Immutable records (asset versions, artifacts, prompt revisions, candidate sets, decisions, publication and import
  receipts, config snapshots) are never rewritten; their ids and hashes stay historical truth (I15).
- Journal schema changes are additive migrations (`journal.py` `_MIGRATIONS`, currently v3). From WP1.2 the journal
  refuses to open a `user_version` newer than it knows.
- Direct mode (`STUDIO_EXECUTION=direct`) keeps today's execution paths until WP2.10. The only approved behaviour
  change in direct mode is the lease safety fix (WP2.1b, below).
- Moved modules leave re-export shims until direct mode is removed.

## Inventory at 47c20db

### Identities (`packages/assetstudio_core/ids.py`)

Format `{prefix}_{16 crockford base32}`; random (`secrets.randbits(80)`) or derived (`blake2b` of prefix + parts, so
replays produce the same id). Prefixes: `prj cat shot ast ver bat itm prm cs cnd qa dec run op art imp exp ref style
qrs job bch brn wav stk pas att sty san xpl xrn cmd sel upl aud fam vdr vpl srs vsa div row jrf med`. Derived by
design: `ast`/`ver` (from the publication op), `fam`, `med` (content sha256), stage task ids (logical key), artifact
ids of stage outputs (`derived_id("art", …)`). Legacy Jobs keep `bat_` ids under `batches/`.

New prefixes planned: runner, runner group, registration token, session, attempt, upload (chosen in WP1.1; they must
not collide with the list above).

### Artifact role contracts (`packages/assetstudio_core/contracts.py`)

| Kind | Required | Optional / patterns |
|---|---|---|
| model3d | model | preview, meta |
| concept_art, sprite | image | preview, meta |
| icon | image | preview, meta, `icon_\d{1,5}` |
| material | base_color | preview, meta, preview_tiled, normal, roughness, metallic, ao, height |
| sprite_sheet, vfx_flipbook | atlas, meta | preview, `frame_\d{4}` |

Checked by `publication.publish` before any write. P3 adds versioned source/delivery profiles beside these; the
legacy contracts stay valid and unchanged.

### Receipts and idempotency

| Record | Where | Purpose |
|---|---|---|
| Publication receipts | `publications/<op>.json` | retry of a publish returns the same asset/version |
| Import receipts | `imports/<imp>.json` | replay-safe imports |
| Command intents | journal `command_intents` | durable intent → idempotent effects → recorded response; replayed at startup |
| Scoped commands | journal `scoped_commands` | idempotency keys scoped by project + action, bound to the whole request |
| Stage tasks | journal `stage_tasks` (logical key unique) | idempotent task creation; the only task-state authority |
| Model passes | journal `passes` | lane, residency, tasks, load counters per pass |
| Lane epochs | journal `lane_epochs` | monotonic GPU1 grant epochs (never reused after restart) |
| Engine execution ids | ComfyUI prompt ids (`engine_prompt_id`), worker3d execution ids on the BuildRun | reconcile a lost submit by lookup |

### Model profiles

`config/models.lock.yaml` (schema 1): per model repo, revision, local_dir, licence, roles, service, gated, files with
size + sha256. Residency signatures (`coordinator/stages/base.py`) hash repo + revision + files. Verification today
runs in Studio against `STUDIO_MODELS_ROOT` (`models.py`), size-only by default, full hash on request.

### Media references

`media/<med_…>.json` (id from content sha256) → `reference` + `preview` artifacts; guidance only; archive hides, never
deletes. Frozen plans must bind artifact ids + hashes, not display metadata.

### API families (routers)

`/api/v1` projects, config, storage, runtime, operations, events (SSE) · `/api/v1/projects/{id}` library, imports,
media, variants, variant plans, legacy batches · `/api/v2/projects/{id}` jobs, batches, runs, waves · `/api/v2`
tasks and passes. Mutations require `X-AssetStudio: 1` and same-origin `Origin` (`main.py`). Runner routes will live
under `/api/runner/v1` with their own authentication (R3) and are the only CSRF exemption.

## Execution mapping (old → new)

| Concern | Direct mode (today) | Node mode (target) |
|---|---|---|
| Engine calls | stage → singleton adapter (`studio.engine/aux/worker3d`) → engine HTTP | stage → remote adapter → attempt → runner → engine |
| Scheduling unit | lane thread runs a residency pass of tasks | continuation pool runs tasks; each call placed independently with slot affinity |
| GPU ownership | Studio `GpuLane` for GPU1 (aux + worker3d) | runner-local `GpuLane` per slot + R7 recovery barrier; Studio holds device claims |
| Model readiness | Studio hashes `/models` | runner verifies the pinned catalog; Studio aggregates per operation |
| Result durability | worker3d spool until Studio `ack` | runner spool until disposition receipt; worker3d acked after runner spool fsync |
| Restart | running tasks requeued; handlers reconcile by engine ids | tasks with live attempts go to `reconciling`; never re-placed while uncertain |
| Auth | CSRF header + Origin, loopback trust | + runner keys/tokens (R3); profile P adds Traefik + Authelia and roles (R16) |

## Cutover and rollback (R15)

1. Deploy with `STUDIO_EXECUTION=direct` (default); verify the journal migration.
2. `assetstudio execution switch --to nodes` pauses admission, waits for quiescence (no running/reconciling task, no
   non-terminal attempt), records `execution_mode=nodes`, resumes.
3. Rollback to direct mode is possible only through `switch --to direct` on a quiesced journal. Starting a
   direct-mode Studio against a node-active journal with live work is refused.
4. After WP2.10 direct mode no longer exists; rollback means restoring a pre-cutover backup (journal + project +
   instance auth store), accepting that work done after the backup must be redone.

## Behaviour changes log

| WP | Change | Modes | Reason |
|---|---|---|---|
| WP0.2 | wrong-shaped glTF JSON is rejected with a field path in every GLB reader | all | A31: previously AttributeError/TypeError |
| WP2.1b | `GpuLane.acquire` drains the target worker too unless it acknowledged a release since (first grant after a restart unloads it once); `Lease.grant` refuses a new epoch while work of an older epoch is active | all | a surviving request of the target worker could overlap a new grant |
