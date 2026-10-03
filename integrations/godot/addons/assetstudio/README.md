# AssetStudio addon (AS-06 client core, AS-07a project install)

Installs exact, verified asset versions from an AssetStudio library into a Godot 4.x project, pins them in a lock
file and restores them byte-identically on any machine. Design: `docs/integration/as-07-08-design.md`.

Status: portable GLB (`portable_glb_v1`) install, lock, restore, verify, `add`, `finalize` with slot resolution and
material policies, `update`, `rollback`, `set-policy` and the editor plugin (dock, Place, drag adapter, update review).
`godot_static_source_v1` (editable source) install, restore and verify: see "Source packages" below. AS-09 adds
`publish` (static scene -> new asset / new version, explicit commit): see "Publishing a scene".

Consumer guide (install, credentials, flows, export, troubleshooting): `docs/integration/godot-consumer.md`.
Compatibility since the v1 freeze: `docs/integration/compatibility.md`.

## Install

Copy `addons/assetstudio/` into the consumer project (or unzip the archive from `scripts/package_addon.py`). No plugin needs to be enabled for the CLI. Core scripts are
loaded with `preload` paths and declare no `class_name`, so they cannot collide with your global classes.

## CLI

```text
godot --headless --path <project> --script res://addons/assetstudio/cli.gd -- <command> [options]

connect  --server-id <uuid> --url <base_url> --token-file <path> [--allow-insecure-lan]
restore  --locked [--offline] [--trust-shaders]
verify   --locked --offline
add      --library <prj_..> --asset <ast_..> --version <ver_..> [--binding <id>] [--profile <id> | --preserve]
         [--representation portable_glb_v1|godot_static_source_v1] [--trust-shaders]
finalize [--binding <id>] [--reapply]
set-policy --binding <id> (--profile <profile_id> | --preserve | --override)
update   --binding <id> --version <ver_..> [--new-binding <id>]
rollback --binding <id>
prune-deliveries [--dry-run | --apply]
export-preflight [--preset <name>] [--offline]
publish  --scene <res://x.tscn> --library <prj_..> [--new-version-of <ast_..> --expected-current <ver_..>] [--commit]
         [--name <text>] [--category <id>] [--tags a,b] [--licence <text>] [--out <dir>] [--fresh]
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
- `prune-deliveries` lists (default / `--dry-run`) or, with `--apply`, deletes managed delivery directories
  `<managed_root>/<asset_key>/<manifest_sha256>/` that no lock dependency delivery references any more (for example
  after `update` or a removed binding). It runs as one coordinator transaction (mutex, crash-safe, all-or-nothing),
  reads the lock inside it, and never touches the blob cache or unknown/`.staging` entries. A later `restore --locked`
  or `rollback` reinstalls what it needs.
- `export-preflight` prints one JSON report (`ok`, `checked`, `problems[{code,message}]`) and exits 1 if the project
  must not be exported: unrestored/modified/unimported managed files, unfinished mutation, pending finalize, edited
  wrappers, presets that do not exclude private paths, scenes referencing addon editor/network scripts. Read-only and
  offline. Gate real exports with `scripts/godot_export_wrapper.py` (restore, import, preflight, export).
- Every command first recovers a crashed transaction.

## Source packages (`godot_static_source_v1`)

`add --representation godot_static_source_v1` (and `restore`, which installs whatever representation the lock binds)
installs an editable source delivery instead of a GLB. Installation never touches the project before the archive is
validated:

1. `project/as_source_package.gd` validates `source.zip` from its bytes (spec `static-source-package.md` §1-§5):
   ZIP container (methods, encryption, symlinks, names, case-fold duplicates, limits and ratio), exact member set and
   hashes against `source_manifest.json`, manifest shape, the Godot text-format subset (section/type allowlists, no
   scripts/connections, inert values, every `ext_resource` path and shader `#include` mapped), reachability and
   capabilities, packaged GLB self-containment. Errors carry the server codes and `details.detail` slugs of
   `fixtures/INDEX.json` (`unsafe_package`, `integrity_mismatch`, `resource_limit`, `unsupported_source_dependency`).
2. `source.zip` is kept byte-identical in the managed delivery directory.
3. `project/as_source_relocator.gd` writes the derived tree `source/` below it with a line-oriented parser/serializer
   (`as_godot_text.gd`), never a global text replacement: `ext_resource path=` goes through the package `resource_map`
   (`package_file` -> `res://<delivery dir>/source/<path>`, `asset_dependency` -> the installed entrypoint of that
   dependency's managed delivery), `uid=` is removed from the `gd_scene`/`gd_resource` header and every
   `ext_resource` (so two versions that share original UIDs coexist; the path fallback stays valid), shader
   `#include "res://..."` lines are mapped. Untouched statements and binary members keep their exact bytes. A
   reference without a map entry, or a `res://`/`uid://` string anywhere else, is refused.
4. Everything is staged and moved in by the coordinator transaction, like a portable delivery: a failure or crash
   leaves a previous install untouched.

- Delivery layout: `assets/library/<asset_key>/<manifest_sha256>/{source.zip, source/..., receipt.json}`.
- Receipt (canonical JSON): the portable fields (`files` = `source.zip` only) plus `source`: `entry_scene`,
  `source_manifest_sha256`, `shader_trust`, `original_files` (path/sha256/size as the package declared them) and
  `installed_files` (path relative to the delivery dir, sha256, size, `original_sha256`, `rewritten`). `verify` checks
  the archive and every installed (derived) file; a modified rewritten file is reported by path.
- Shader-bearing packages (`shader_source`) are source-only and never executed on iPad: they are refused with
  `unsafe_package` ("shader trust required") unless `--trust-shaders` is given to `add`/`restore`.
- Asset dependencies must be in the lock closure (`unsupported_source_dependency` otherwise) and pinned by the delivery
  manifest; the dependency is resolved to its installed `portable.glb`.
- The wrapper `assets/prefabs/<binding_id>.tscn` instances the derived entry scene. A source binding is not marked
  `pending_import`, material profiles do not apply to it (`--profile` is refused; `finalize`/`set-policy` on it fail with
  `unsupported_representation`), and `update`/`rollback` still handle `portable_glb_v1` only.
- Client-side limits: Unicode NFC cannot be computed, so every non-ASCII member name is rejected as `invalid_path`
  (the safe path pattern excludes them anyway); member sizes are bounded by the declared sizes (ZIPReader) rather
  than streaming-counted; ZIP CRC-32s are not checked (declared files are pinned by sha256); `placement` /
  `conversion_report` deep schema, descriptor surface cross-checks, node-count/instancing limits and mesh-level GLB
  budgets stay server-side (image/GLB checks are header/JSON-chunk only).

## Publishing a scene (AS-09)

`publish` sends a static Godot scene to a library as a new asset or as a new version of an existing one. It never
runs by itself: there is no save watcher, no automatic upload and no AI job. Only `.assetstudio/publish/` is written
locally (build output and the journal, git-ignored); the project, the lock and the open scene are never modified.

```text
godot --headless --path <project> --script res://addons/assetstudio/cli.gd -- publish \
  --scene res://scenes/hut.tscn --library prj_... [--name Hut] [--tags a,b] [--licence CC0]      # build + preview, stops here
  ... --commit                                                                                   # then commit (new asset)
  ... --new-version-of ast_... --expected-current ver_... --commit                                # new version, compare-and-swap
```

1. **Build.** The saved scene file is read from disk (never the open editor tree) and instantiated into a private
   holder, bypassing the resource cache. The closure is walked over the text files (`ext_resource`, shader
   `#include`) with `ResourceLoader.get_dependencies` as a cross-check; `uid://` and path forms are both resolved
   (the reference as written stays the `resource_map` key). References into an installed AssetStudio delivery
   (`assets/library/<key>/<manifest_sha256>/...`) become `asset_dependency` entries pinned from `assetstudio.lock.json`.
   Scripts, animation, skeletons/skins, particles, signal connections, custom classes, lights/cameras, binary
   `.scn`/`.res`, unsupported file types, unsaved or missing dependencies and a missing lock entry **block**
   publication with the reason (nothing is dropped silently). Text resources and binaries are copied unchanged.
2. **Package.** `source.zip` (source_manifest.json + closure, sorted members, fixed 1980-01-01 timestamps, deflate or
   stored) and `portable.glb` (GLTFDocument export; CSG baked to a mesh; collision and markers are source-only;
   ShaderMaterials replaced by a tinted/neutral StandardMaterial3D and disclosed as `custom_shader_approximated` and
   in `conversion_report`). The GLB is parsed back (no external uris, skins or animations); iPad budgets are reported
   as warnings. Placement: anchor = the `GroundAnchor` Marker3D, else bottom centre; footprint radius from the bounds;
   scale `0.5..2`, height offset `-0.1..0.5` and `FOLLOW_TERRAIN` as defaults; one material slot per distinct material.
3. **Preview.** Multipart upload (`source`, `portable`, `descriptor`, `report`); the CLI prints the review JSON
   (server validation, warnings, budget, conversion report, descriptor draft, hashes).
4. **Commit** (`--commit`, or the dock's Commit button). `expected_current_version` is mandatory for an existing target
   (`stale_pointer` is reported as a conflict with the current version; the local source is untouched). The
   idempotency key is generated once per reviewed intent and stored in `.assetstudio/publish/journal.json` before the
   request is sent. After a lost response the operation is queried first (`publication-operations/{key}`), so a
   retry returns the existing version instead of publishing twice; repeating the same command is answered from the
   journal (`--fresh` publishes again). An expired preview is re-uploaded with a new key unless the old key committed.

Dock: **Publish scene...** opens a form (name, category, tags, licence, "new version of the selected asset", "selected
node only"), builds and uploads the preview, shows the review (server validation, omissions, approximations,
descriptor draft) and only then offers **Commit**. After a stale base version it shows the conflict and offers
**Publish as new asset**. The scene must be saved (unsaved scenes are listed and refused). A selected subtree is
saved from a duplicate to a temporary scene; the open scene is not touched.

Limits: textures must decode to images in the editor (the exporter embeds them as PNG); the multipart body is assembled in memory (bounded by `publication_upload_max_bytes`, checked from the file
sizes before any byte is read), so peak memory is about twice the upload; unsaved resource edits inside the editor
(other than scenes) cannot be detected; opaque `.glb` instances report their surfaces as (instance node, running index);
Unicode node names/paths outside `A-Za-z0-9_-`/`A-Za-z0-9_.-` block publication (rename them).

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
                                                (source: source.zip, source/ derived tree, receipt.json)
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
python3 integrations/godot/tests/run_publish_tests.py       # publish E2E: fixtures-equivalent scenes, server checks via uv, commit/CAS/recovery
python3 integrations/godot/tests/run_source_tests.py        # source packages: install from fixture zips, fresh .godot import, load
python3 integrations/godot/tests/run_export_tests.py        # export-preflight scenarios + export wrapper (real macOS export when the template exists)
```

## Deviations and limits

- Godot strings cannot hold U+0000, so the canonical writer cannot encode it (the shared vectors include it; the
  GDScript test skips that single case). Lock documents never contain it.
- `assetstudio.project.json` is parsed leniently (any valid JSON formatting) and written canonically; the lock
  must already be canonical to be read.
- The `.import` pre-seed is evidence of import only: `verify` checks existence, not its content; `export-preflight` additionally requires a `[remap]` result whose `.godot/imported` file exists.
- The receipt lists the files it was written with; `verify` cannot detect an attacker who rewrites both a file and
  its receipt (the lock pins only the manifest hash, and the manifest is not stored in the project).
- `ASAssetResolver.prepare` gained an optional `pin_delivery_id`; restore uses it so a server that also offers a
  newer profile can never cause a substitution.
- A pinned `prepare` (restore, dependency closure) is served from the blob cache with no network request when the
  manifest and every file are cached and re-hash correctly (a corrupt blob is evicted and re-downloaded); unpinned
  prepares stay network-first.
- The resolver refuses a manifest (root or any dependency) whose `required_capabilities` are not in
  `ASSchema.SUPPORTED_CAPABILITIES` with `unsupported_contract`, and honours the resolve `representations` map
  (`unsupported_representation` fallback) when the server sends it.
- A crash before the transaction journal is written leaves `assets/library/.staging/<txn>` garbage; it is never
  imported and is safe to delete.
