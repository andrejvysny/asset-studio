# Godot consumer guide (AS-10)

How any Godot 4.x project consumes AssetStudio assets, builds reproducibly and exports. Addon reference:
`integrations/godot/addons/assetstudio/README.md`. Contracts: `contracts/godot-integration/v1/`.

`<project>` = your Godot project directory. `GD` = `godot --headless --path <project> --script res://addons/assetstudio/cli.gd --`.

## 1. Install the pinned addon

```bash
python3 scripts/package_addon.py --out-dir dist          # in the asset-studio checkout
# dist/assetstudio-addon-0.2.1.zip, .zip.sha256, .manifest.json (version, source commit + dirty flag, contract, per-file sha256)
cd dist && shasum -a 256 -c assetstudio-addon-0.2.1.zip.sha256
unzip -o assetstudio-addon-0.2.1.zip -d <project>         # creates <project>/addons/assetstudio/
```

The build is deterministic (same inputs, same bytes). Commit the addon, or pin the archive sha256 in your CI. A
release build must report `"dirty": false` in the manifest. No plugin needs to be enabled for the CLI.

## 2. Project files

| File | Tracked | Purpose |
|---|---|---|
| `assetstudio.project.json` | yes | server id, libraries, managed root; no secrets. Created by `connect` |
| `assetstudio.lock.json` | yes | exact, hash-pinned dependency closure (this repo's frozen `project-lock.schema.json`) |
| `assets/prefabs/<binding>.tscn` | yes | generated wrapper scenes |
| `assets/library/`, `.assetstudio/` | no | managed deliveries and bookkeeping, restored from the lock |

```gitignore
/assets/library/
/.assetstudio/
```

## 3. Credentials (never in the repo)

Create a token on the server (printed once), keep it outside the project with mode 0600, then:

```bash
assetstudio integration token create my-game --library prj_... --scope assets:read   # add assets:publish to publish
(umask 077; printf %s "asi_..." > ~/.config/assetstudio/my-game.token)
GD connect --server-id <uuid> --url https://studio.example --token-file ~/.config/assetstudio/my-game.token
```

Endpoint and token are stored in Godot's user data dir (`user://assetstudio/`), not in `res://`. Tokens are never read
from argv and never printed. CI: write the token file from a secret before `connect`, delete it afterwards.

## 4. Daily flows

```bash
GD add --library prj_... --asset ast_... --version ver_... --preserve    # install exact version, lock, wrapper scene
godot --headless --editor --path <project> --import                      # import before first use
GD finalize                                                              # resolve slots, apply material policy
```

- Source (editable) delivery: `add ... --representation godot_static_source_v1` (shader packages need `--trust-shaders`).
- Place: use the editor dock (**Place**), or instance `res://assets/prefabs/<binding>.tscn` in a scene.
- Update: `GD update --binding <id> --version ver_...` (or `--new-binding <id>`), import, `GD finalize`; `GD rollback --binding <id>`.
- Material policy: `GD set-policy --binding <id> (--profile <id> | --preserve | --override)`.
- Publish a scene: `GD publish --scene res://scenes/hut.tscn --library prj_... [--name Hut]` (preview), add `--commit`
  to publish; `--new-version-of ast_... --expected-current ver_...` for a new version.
- Fresh clone / CI: `GD restore --locked`, then the headless import. `GD verify --locked --offline` checks files only.

## 5. Export

Export presets must exclude private files and keep the managed root in. Minimal `exclude_filter` (explicit even
though Godot skips dot directories):

```text
.assetstudio/*,*.token,server.token,connections.json,credentials.json,.env,addons/assetstudio/*,export_presets.cfg
```

Add `textures/vram_compression/import_etc2_astc=true` under `[rendering]` for macOS/iOS universal exports.

`export-preflight` (read-only, never touches the network) prints one JSON report and exits 1 on any problem:

```bash
GD export-preflight --preset "macOS" --offline
```

It checks: every lock root's dependency closure is installed with matching receipt hashes (installed and rewritten
files); every Godot-imported file has a real import result (`.import` with `[remap]` and the `.godot/imported` file);
no unfinished transaction or pending `finalize`; generated wrappers unedited; the preset(s) exclude the private paths
above and do not exclude managed deliveries; no scene, resource or `project.godot` references the addon's `editor/`,
`project/`, `plugin.gd`, `cli.gd` or network scripts (an exported game never falls back to HTTP).

Gated export (stops at the first failure, exports nothing unless all passed):

```bash
python3 scripts/godot_export_wrapper.py --project <project> --preset "macOS" --out build/game.zip [--offline] [--godot PATH]
```

Steps: `restore --locked` -> headless import -> `export-preflight` -> `godot --headless --export-release`. Use
`--preflight-only` to stop after a passing preflight. Editor export hooks only add diagnostics; the wrapper is the gate.

### Offline export

1. Online once: `GD restore --locked` (fills the blob cache and pins locked blobs).
2. Later without network: `python3 scripts/godot_export_wrapper.py ... --offline` (restore uses the cache only).
3. A cache miss fails with `unavailable`/`integrity_mismatch`; fix by running step 1 online.

## 6. Troubleshooting

| Symptom (problem code) | Fix |
|---|---|
| `dependency_integrity: not installed` | `GD restore --locked` |
| `dependency_integrity: ... is missing or modified` | A managed file was edited. Delete the delivery dir and `restore --locked` (restore never overwrites) |
| `import_incomplete` | `godot --headless --editor --path <project> --import` |
| `pending_finalize` | `GD finalize` |
| `wrapper_modified` | Revert the wrapper or regenerate: `GD finalize --binding <id>` (hand edits conflict) |
| `interrupted_mutation` | `GD restore --locked` recovers a crashed transaction |
| `export_presets_missing` / `preset_missing_exclude` | Add/fix the preset; see the `exclude_filter` above |
| `preset_excludes_managed` | Remove the filter that matches `assets/library/*` |
| `forbidden_reference` | Remove the scene/autoload reference to addon editor/network scripts |
| `invalid_project_file` | `assetstudio.lock.json` / `assetstudio.project.json` missing or not canonical; re-run `connect`/`add` |
| `Cannot export for universal or arm64 ... ETC2 ASTC` | enable `import_etc2_astc` (see above) |
| `unsupported_contract` | Addon older than the asset's required capabilities; install a newer addon |

Gap: a `.glb` or texture that Godot imported but whose `.godot/imported` file was later pruned shows as
`import_incomplete`; re-import.
