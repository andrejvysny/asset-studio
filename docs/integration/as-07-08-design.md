# AS-07 / AS-08 design: project install, lock, coordinator, editor dock

Status: design for implementation (2026-10-01). Specs: INT-SPEC-1.1 §3, §6, §7, §11 (coordinator); AS-SPEC §4–§6.
Scope of this round:
- AS-07a: portable GLB install, project lock, transaction coordinator, restore and verify CLI.
- AS-08: editor dock, placement, wrappers, material policies and update review.

Deferred to AS-07b, together with AS-09: `godot_static_source_v1` relocation, meaning the zip extraction, the tscn/tres reference rewriter and the UID remapping. The fantasy-base library holds portable GLBs only.

## 1. Consumer project layout

```text
<project>/
  assetstudio.project.json          tracked; no secrets (§2)
  assetstudio.lock.json             tracked; ProjectAssetLockV1, canonical bytes (§3)
  assets/library/<asset_key>/<manifest_sha256>/   git-ignored managed deliveries
      portable.glb                  copied from the verified blob cache, byte-identical
      portable.glb.import           pre-seeded by the installer (§4.3), then owned by Godot
      receipt.json                  installation receipt (§4.2)
  assets/library/.staging/<txn>/    git-ignored; dot-prefixed so the Godot filesystem never imports it
  assets/prefabs/<binding_id>.tscn  tracked wrapper scenes (§5)
  .assetstudio/                     git-ignored; journal + mutex (§6)
  integration/material_profiles/<profile_id>.json   tracked, game-owned (§7)
```

Credentials and endpoints stay in `ASConnectionRegistry` under `user://assetstudio/`. That directory is per project, outside `res://`, and never exported.

**Deviation from AS §5.3, which says to stage "outside res://":** staging uses the dot-prefixed `assets/library/.staging/`.
- Godot ignores dot directories, so partially written files are never imported.
- The staging directory sits on the same filesystem as the destination, so the final `rename` is atomic.

## 2. `assetstudio.project.json` (addon-owned format, schema_version 1)

```json
{"schema_version":1,
 "server_id":"<uuid>",
 "libraries":[{"library_id":"prj_…","label":"fantasy-base"}],
 "managed_root":"res://assets/library",
 "prefab_root":"res://assets/prefabs",
 "material_profiles_dir":"res://integration/material_profiles",
 "default_material_policy":{"mode":"preserve","profile_id":null}}
```

- Parsing is strict: unknown keys are errors, and roots must start with `res://` and contain no `..`.
- It is written with the canonical writer.

## 3. Lock

The lock is `assetstudio.lock.json` and uses the frozen `project-lock.schema.json`. It is parsed and validated in GDScript with every rule from `packages/assetstudio_core/project_lock.py`:
- key equals `asset_key(asset_ref)`
- requires contains no duplicates, is closed, and is acyclic
- each binding's representation is present in its dependency's deliveries
- root keys exist and are unique
- material policy pairing is valid
- `update_policy` is `prompt`

Bytes are canonical JSON, identical to Python `canonical_v1.canonical_bytes`, so Python and GDScript produce identical lock files. GDScript gets `core/as_canonical_json.gd`, a dedicated writer that does **not** use `JSON.stringify`:
- sorted keys, `,` and `:` separators, UTF-8
- `ensure_ascii=False`
- escapes `\" \\ \n \r \t \b \f`, with any other code point below U+0020 written as lowercase `\u00xx`
- no escaping of `/`, U+007F, U+2028 or any other non-ASCII
- floats and non-string keys are rejected

Readers re-encode the parsed document and compare it with the file, so non-canonical bytes are rejected.

Binding identity:
- `binding_id` is a slug, `<asset-name-slug>-<first 8 hex of asset_key>`, made unique with `-2`, `-3` and so on.
- Each binding owns one root `{owner_kind:"scene_binding", owner_id:binding_id, asset_keys:[key + closure]}`.
- Roots with `world_generation` belong to World Painter Apply. They are never removed by this addon.

## 4. Install (AS-07a)

### 4.1 Restore (`ASRestore`)

1. Recover any pending transaction.
2. For each dependency, prepare the locked representation of every binding that references it, and its `requires` closure, through `ASAssetResolver.prepare(exact_ref, representation)`.
3. **Exact pin check.** The prepared `delivery_id` and manifest `raw_sha256` must equal the lock values. A mismatch is `integrity_mismatch`. The client must never substitute a different delivery, even one with a newer profile.
4. Install a delivery only if it is not already installed and verified (§4.2).
5. Write nothing else. The lock is the input to restore, never its output.
6. With `--offline`, the resolver runs with `offline_only = true`, so nothing touches the network.

### 4.2 Materialize (`ASInstaller`)

All file operations go through a coordinator transaction.

1. Copy each manifest file from its blob into `.staging/<txn>/`. Verify size and sha256.
2. Write `receipt.json` in canonical bytes:

   ```json
   {"schema_version":1,
    "asset_key", "asset_ref", "delivery_id", "representation",
    "manifest_sha256", "descriptor_sha256",
    "files":[{"path","sha256","size"}],
    "installer_version"}
   ```

3. Rename the staging directory to `<managed_root>/<asset_key>/<manifest_sha256>/`.
4. An existing target directory is verified: receipt hashes plus file hashes. On a mismatch, report it as tampered and do not overwrite.
5. Never hardlink to the cache.
6. Verification:
   - `verify --offline` recomputes every file hash against its receipt.
   - It checks that each lock delivery has an installed directory.
   - It checks that `portable.glb.import` exists, as evidence the file has been imported. It does not interpret the import cache.

### 4.3 Import settings

Before Godot first sees `portable.glb`, the installer writes `portable.glb.import` containing only a `[params]` section with:
- `array_mesh/deduplicate_surfaces=false`, so glTF primitive *p* stays surface *p*
- `meshes/generate_lods=true`
- `materials/extract=0`
- `nodes/apply_root_scale=true`
- `_subresources={}`

Godot fills in `[remap]` and `[deps]` on import.

The headless import step is the spec command: `godot --headless --editor --path <project> --import`. Inside the editor the plugin calls `EditorFileSystem.scan()`, waits for `filesystem_changed` / `resources_reimported`, and processes one queue at a time.

## 5. Wrappers (AS-08)

The wrapper scene is `<prefab_root>/<binding_id>.tscn`:

```text
<BindingName> (Node3D)                 metadata: assetstudio_binding=<binding_id>, assetstudio_asset_key=<key>
└─ Model  (instance of res://assets/library/<key>/<msha>/portable.glb)   position = -placement_anchor
```

- Node position equals the world anchor (INT §4.2). The model is never re-centred, re-scaled or re-grounded.
- `Model` is marked an editable instance only when a material policy writes surface overrides. Overrides are stored as `surface_material_override/<n>` on the imported `MeshInstance3D` nodes. Patched materials are embedded as `sub_resource`.
- Game logic, collision and siblings live in scenes that *instance* the wrapper. The wrapper is regenerated by the addon and must not be edited by hand.
- Before rewriting a wrapper, the addon compares the wrapper file hash against the one recorded at write time in `.assetstudio/wrappers.json`. A differing hash is a conflict and is reported, never overwritten.

### 5.1 Slot resolution

Descriptor slot surfaces are `{mesh, primitive}` glTF indices. Steps:
1. Parse the GLB JSON chunk, from bytes already verified, to get the glTF mesh names.
2. Instantiate the imported scene. For each `MeshInstance3D` whose `mesh.resource_name` equals a glTF mesh name, surface index = primitive index (deduplication is off, §4.3).
3. Fallback for an ambiguous or unnamed mesh: match by order of first appearance in the node tree.

If a slot still can't be resolved, it is reported as unmapped and keeps its source material.

## 6. Mutation coordinator (`ASMutationCoordinator`)

This is the single writer for the lock, `assetstudio.project.json`, wrappers, managed directories and scene-file rewrites that the addon performs. Future World Painter Apply must use it too.

**Mutex:** `.assetstudio/lock/` created with `DirAccess.make_dir`, which is atomic. The holder writes `owner.json` with pid, time and operation. A stale lock whose pid is dead may be broken, and that is logged.

**Transaction:**
1. Intent: `.assetstudio/txn/<id>/intent.json`, listing ops `{kind: write|rename_dir|delete, path, before_sha256|null, after_sha256|null, staged}`.
2. Stage the new content into `.assetstudio/txn/<id>/stage/`, or for directories into `<managed_root>/.staging/<id>`.
3. Back up each existing target to `.assetstudio/txn/<id>/backup/`.
4. Apply the ops in order, using `rename` where possible.
5. Write the commit marker `.assetstudio/txn/<id>/COMMITTED`.
6. Clean up the transaction directory.

**Recovery** runs at plugin enable and at the start of every CLI command:
- Transaction with a commit marker: finish the cleanup.
- Transaction without one: restore the backups, delete the newly created targets, and verify the `before_sha256` values.

After recovery the project is in exactly the old state or exactly the new state.

**Crash injection:** a test hook `ASMutationCoordinator.fail_after_step = N` stops after step N, simulating a crash. Tests cover every N.

Editor Undo (`EditorUndoRedoManager`) covers scene placement only. Disk transactions are rolled back with the explicit **Restore previous version** action, which re-applies the previous binding from `.assetstudio/history.json`, a list of committed transaction summaries with before and after hashes.

## 7. Material policies

The modes are those of lock `material_policy`:
- `preserve`: no overrides.
- `project_mapping`: apply the profile `material_profiles_dir/<profile_id>.json`. `profile_sha256` is the sha256 of that file's raw bytes. A changed profile shows a "profile changed, re-apply?" prompt.
- `override`: an explicit per-binding override file, `material_profiles_dir/overrides/<binding_id>.json`, in the same rule format. `profile_id` and `profile_sha256` are null.

Profile format (game-owned data; the addon only interprets it):

```json
{"schema_version":1,"profile_id":"fantasy_nature","rules":[
  {"match":{"role":"foliage"},"material":"res://materials/nature/nature_foliage.tres"},
  {"match":{"slot_id":"m_solid"},"material":"res://materials/nature/nature_wood.tres"},
  {"match":{"role":"solid"},"patch":{"metallic":0.0,"metallic_texture":null}}]}
```

- The first matching rule wins.
- `match` keys are `slot_id`, `role` or both.
- A rule has `material` (a `res://` path to a Material resource) or `patch` (duplicate the *source* material and set allowlisted properties), not both.
- Patch allowlist: `metallic`, `metallic_texture` (null only), `roughness`, `cull_mode`, `vertex_color_use_as_albedo`, `albedo_color`, `transparency`, `alpha_scissor_threshold`.
- A slot no rule matches is preserved and reported as unmapped.
- Source textures are never discarded implicitly. A `material` rule is an explicit game choice.

## 8. Editor plugin (AS-08)

The plugin is `plugin.cfg` + `plugin.gd`, an EditorPlugin. On enable it does:
- coordinator recovery
- adds the dock
- starts the ChangeWatcher only when a connection is configured

**Dock** (built in code, native Controls):
- connection status
- library selector
- search, category and tags (server-side query)
- asset list with thumbnail, name, exact version and state badge (Remote, Downloading, Preparing, Ready, Update available, Unavailable, Unsupported)
- details pane with descriptor summary, slots, warnings and installed bindings

**Actions:**
- **Install**, which creates the binding, the lock entry and the wrapper.
- **Place** (spec decision: button first). It instantiates the wrapper scene:
  - under the selected Node3D, or the scene root if none is selected
  - at the 3D viewport centre ray's hit, using `get_editor_viewport_3d(0)` camera plus a physics ray, else the origin
  - through `EditorUndoRedoManager` with the edited scene root as context: `add_do_method` add_child + set_owner, `add_undo_method` remove_child, and `add_do_reference`
  - unready assets are never placeable
- **Drag adapter** (`editor/as_drag_adapter.gd`): `_get_drag_data` returns Godot's native `{"type":"files","files":[wrapper_path]}` payload, but only for Ready bindings. Placement and Undo are then owned by the editor's own file-drop. This is verified manually; the result is recorded in `docs/integration/baseline.md` (AS-00 GUI spike).

**Update review:** `asset_current_changed` from the ChangeWatcher marks bindings whose asset has a newer current version than the locked one. "Latest" is resolved once into an exact version. The dialog shows:
- current versus target version
- descriptor diffs: anchor, bounds, scale/height ranges, slots added/removed/role changes, collision
- affected bindings and instances

Apply options, both run as one coordinator transaction:
- **Update binding** (all instances). Install the target delivery, rewrite the wrapper to the new managed path, update the lock dependency, and remove the old dependency only when no root references it any more.
- **Update selected instances.** Create a new binding (new wrapper) for the target version and swap the selected wrapper instances in the edited scene via `EditorUndoRedoManager`, preserving their transforms. The old binding and other instances are unchanged.

Declining dismisses the prompt until the next server change.

## 9. CLI (`addons/assetstudio/cli.gd`, extends SceneTree)

```text
connect --server-id <uuid> --url <base_url> --token-file <path> [--allow-insecure-lan]
restore --locked [--offline]
verify  --locked --offline
add     --library <prj> --asset <ast> --version <ver> [--binding <id>] [--profile <profile_id>|--preserve]
```

- Exit codes: 0 means ok, 1 a failure (unavailable, integrity, unsafe, unsupported), 2 a usage error.
- Tokens are never accepted in argv.
- `add` installs and writes the lock and wrapper in one transaction.
- `add` cannot finish wrapper slot resolution before import. It writes the wrapper without material overrides and marks the binding `pending_import` in `.assetstudio/state.json`. A second command, `finalize`, runs after import, applies the material policy and rewrites the wrapper:

```text
godot --headless --editor --path P --import
godot --headless --path P --script res://addons/assetstudio/cli.gd -- finalize
```

## 10. Tests

| Area | Coverage |
|---|---|
| Canonical writer | Byte-equal with Python on generated vectors (`contracts/godot-integration/v1/fixtures/vectors/canonical-json-v1.json`, new and additive) and on every lock fixture. Invalid lock fixtures are rejected for the expected reason. |
| Coordinator | Crash injection at every step leaves the old or the new state. Concurrent mutex. Stale-lock breaking. Recovery is idempotent. |
| Installer and restore | Fake server: install, offline verify, tampered file detected, delivery-id mismatch refused, two versions coexist in distinct paths. |
| Consumer E2E (`integrations/godot/tests/run_consumer_tests.py`) | Temp consumer project with addon and `project.godot`, fake server: connect → add → import → finalize → instantiate wrapper → mesh at the anchor offset, profile-mapped material applied, unmapped slot preserved. Restore from clean (managed dir deleted) gives identical hashes. Runs without World Painter or Terrain3D. |
| Editor | Plugin loads headless with `--editor` (no errors). The placement function is unit-tested with a stub UndoRedo. Drag is a manual check. |
