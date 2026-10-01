# AssetStudio addon (AS-06 client core, AS-07a project install)

Installs exact, verified asset versions from an AssetStudio library into a Godot 4.x project, pins them in a lock
file and restores them byte-identically on any machine. Design: `docs/integration/as-07-08-design.md`.

Status: portable GLB (`portable_glb_v1`) install, lock, restore, verify and a minimal `add`. The editor dock,
material policies (wrapper overrides) and `godot_static_source_v1` relocation are AS-08 / AS-07b.

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
finalize
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
- `restore --locked` installs every locked delivery that is missing, pinned to the exact `delivery_id` and manifest
  sha256 of the lock. A different delivery is `integrity_mismatch`. It never rewrites the lock and never overwrites
  an installed delivery that fails verification. `--offline` uses only the local blob cache.
- `verify --locked --offline` inspects files only (no network objects are created): receipt, file hashes and the
  `.import` file of every locked delivery. Exit 1 lists each problem.
- Every command first recovers a crashed transaction.

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
