# AssetStudio addon (AS-06 client core, AS-07a project install)

Installs exact, verified asset versions from an AssetStudio library into a Godot 4.x project, pins them in a lock
file and restores them byte-identically on any machine. Design: `docs/integration/as-07-08-design.md`.

Status: portable GLB (`portable_glb_v1`) install, lock, restore, verify, `add`, `finalize` with slot resolution and
material policies, `update`, `rollback`, `set-policy` and the editor plugin (dock, Place, drag adapter, update review).
`godot_static_source_v1` relocation is AS-07b.

## Install

Copy `addons/assetstudio/` into the consumer project. No plugin needs to be enabled for the CLI. Core scripts are
loaded with `preload` paths and declare no `class_name`, so they cannot collide with your global classes.

## CLI

```text
godot --headless --path <project> --script res://addons/assetstudio/cli.gd -- <command> [options]

connect  --server-id <uuid> --url <base_url> --token-file <path> [--allow-insecure-lan]
restore  --locked [--offline]
verify   --locked --offline
add      --library <prj_..> --asset <ast_..> --version <ver_..> [--binding <id>] [--profile <id> | --preserve]
finalize [--binding <id>] [--reapply]
set-policy --binding <id> (--profile <profile_id> | --preserve | --override)
update   --binding <id> --version <ver_..> [--new-binding <id>]
rollback --binding <id>
```

Exit codes: `0` ok, `1` failure (unavailable, integrity, unsafe, unsupported, tampered), `2` usage error.

- `connect` reads the bearer token from a file (never from argv), stores endpoint and token in `user://assetstudio/`
  and checks the server identity. It creates `assetstudio.project.json` when missing.
- `add` resolves the exact version (never "latest") plus its dependency closure, installs it, and writes the lock
  dependency, binding, root and a wrapper scene `assets/prefabs/<binding_id>.tscn` in ONE transaction. The wrapper has
  no material overrides yet. The binding is marked `pending_import` in `.assetstudio/state.json`.
- Run the headless import before using the wrapper (a freshly written `.glb` loads only after it):
  `godot --headless --editor --path <project> --import`. Then run `finalize`; in AS-07a it only clears the
  `pending_import` marks and reports (wrapper/material finalize is AS-08).
- `finalize` (after the headless import) resolves the descriptor slots on the imported scene, applies the binding's
  material policy and rewrites the wrapper through the coordinator. Default targets: bindings marked `pending_import`;
  `--binding` regenerates one binding. A `project_mapping` profile whose file changed since it was locked is refused
  (`profile_changed`) unless `--reapply`, which also updates `profile_sha256`. A wrapper whose hash differs from
  `.assetstudio/wrappers.json` (hand edit) is a `conflict` and is never overwritten. Unmapped slots and unresolved
  surfaces are listed and keep their source material.
- `set-policy` changes the lock `material_policy` and the wrapper in one transaction (the model must be imported).
  `--override` reads `integration/material_profiles/overrides/<binding_id>.json` (same format, `profile_id` = binding id).
- `update --version V` re-points the binding (all its instances) to version V; `--new-binding N` creates a new binding
  and wrapper for V and leaves the old one untouched. Both install the exact target delivery, mark the binding
  `pending_import` (run the import, then `finalize`) and prune a lock dependency only when no root references it.
  Managed delivery directories are never deleted.
- `rollback` undoes the last `update`/`rollback` of a binding using the summary kept in `.assetstudio/history.json`
  (it restores the pruned lock dependencies and re-installs from cache or server if needed). Repeating it toggles.
- `restore --locked` installs every locked delivery that is missing, pinned to the exact `delivery_id` and manifest
  sha256 of the lock. A different delivery is `integrity_mismatch`. It never rewrites the lock and never overwrites
  an installed delivery that fails verification. `--offline` uses only the local blob cache.
- `verify --locked --offline` inspects files only (no network objects are created): receipt, file hashes and the
  `.import` file of every locked delivery. Exit 1 lists each problem.
- Every command first recovers a crashed transaction.

## Material profiles

`integration/material_profiles/<profile_id>.json` (game-owned, tracked):

```json
{"schema_version":1,"profile_id":"fantasy_nature","rules":[
  {"match":{"role":"foliage"},"material":"res://materials/nature/nature_foliage.tres"},
  {"match":{"slot_id":"m_solid"},"patch":{"metallic":0.0,"metallic_texture":null}}]}
```

Strict: unknown keys are errors; `profile_id` must equal the file name; each rule has a non-empty `match`
(`slot_id` and/or `role`) and exactly one of `material` (a `res://` path that loads as a Material) or `patch`
(duplicate of the source surface material; keys: `metallic`, `metallic_texture` (null only), `roughness`, `cull_mode`
0..2, `vertex_color_use_as_albedo`, `albedo_color` (`#rrggbb[aa]` or `[r,g,b(,a)]`), `transparency` 0..5,
`alpha_scissor_threshold`). The first matching rule wins. `profile_sha256` = sha256 of the raw file bytes.
Overrides are stored as `surface_material_override/<n>` on the imported nodes of an editable `Model` instance; patched
materials are embedded sub-resources with stable ids. Limitation: a patch of a textured source material embeds
the patched material in the wrapper, but its textures stay `ext_resource` references to the `model_N.png` files Godot
extracts from the GLB (they are not embedded); prefer a `material` rule for textured assets.

## Editor plugin

Enable `Project > Project Settings > Plugins > AssetStudio`. On enable it recovers an interrupted transaction and adds
the dock (left-bottom): connection status (run `connect` once), library selector, server-side search / category /
tags, asset list (thumbnail, name, version, badge Remote / Downloading / Preparing / Ready / Update available /
Unavailable / Unsupported), details pane. Buttons: **Install** (add, editor import, finalize), **Place** (under the
selected Node3D or the scene root, at the 3D viewport centre ray hit else the origin, via EditorUndoRedoManager),
**Review update** (current vs target, descriptor diff; Update binding / Update selected instances / Dismiss) and
**Restore previous version**. Change events (`asset_current_changed`) and **Check updates** resolve "latest" once into
an exact version. The editor calls the same `project/` functions as the CLI.

### Manual checks (not automated)

1. Drag: connect, Install an asset, wait for `[Ready]`, drag its row into the 3D viewport. The wrapper must be instanced
   at the drop point with Undo working. Rows that are not Ready must not start a drag. Record the result in
   `docs/integration/baseline.md`.
2. Look and feel of the dock; thumbnails; Place at a real physics hit (add a collider, aim the viewport centre at it).
3. Update badge from a real server change: publish a new current version, wait for the badge, Review update.

## Layout in the consumer project

```text
assetstudio.project.json     tracked, no secrets (roots, library list, default material policy)
assetstudio.lock.json        tracked, canonical JSON (byte-identical to the Python writer)
assets/library/<asset_key>/<manifest_sha256>/   managed deliveries: portable.glb, portable.glb.import, receipt.json
assets/library/.staging/     in-flight installs (dot directory: Godot never imports it)
assets/prefabs/<binding_id>.tscn   tracked wrapper scenes (generated, do not edit)
.assetstudio/                journal, mutex, history.json, state.json, wrappers.json
```

## Credentials

Endpoint and token live in `user://assetstudio/connections.json` and `credentials.json` (mode 0600 where the OS
supports it), outside `res://` and never exported. The blob cache is `user://assetstudio/cache`; restore pins the
blobs of the locked deliveries so a cache prune keeps them.

## .gitignore for consumers

```gitignore
# AssetStudio managed deliveries are restored from the lock: do not commit them
/assets/library/
/.assetstudio/
```

Commit `assetstudio.project.json`, `assetstudio.lock.json` and `assets/prefabs/`. After a fresh clone run
`restore --locked`, then the headless import.

## Tests

```text
godot --headless --path integrations/godot --script res://tests/run_tests.gd [-- --filter=<substring>]
python3 integrations/godot/tests/run_client_tests.py        # network tests against fake_server.py
python3 integrations/godot/tests/run_consumer_tests.py      # temp consumer project, CLI end to end
python3 integrations/godot/tests/run_plugin_tests.py        # plugin smoke + headless-editor self-test of the dock actions
```

## Deviations and limits

- Godot strings cannot hold U+0000, so the canonical writer cannot encode it (the shared vectors include it; the
  GDScript test skips that single case). Lock documents never contain it.
- `assetstudio.project.json` is parsed leniently (any valid JSON formatting) and written canonically; the lock
  must already be canonical to be read.
- The `.import` pre-seed is evidence of import only: `verify` checks existence, not its content.
- The receipt lists the files it was written with; `verify` cannot detect an attacker who rewrites both a file and
  its receipt (the lock pins only the manifest hash, and the manifest is not stored in the project).
- `ASAssetResolver.prepare` gained an optional `pin_delivery_id`; restore uses it so a server that also offers a
  newer profile can never cause a substitution.
- A crash before the transaction journal is written leaves `assets/library/.staging/<txn>` garbage; it is never
  imported and is safe to delete.
