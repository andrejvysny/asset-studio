# Godot integration: backward-compatibility review (AS-10)

Reviewed 2026-10-02 against the v1 freeze (commit `fffe1cd`, 2026-10-01). Addon 0.2.1, `contract_version` 1, `api_version` 1.

## Frozen contracts

`git status` and `git diff HEAD -- contracts` are empty: no file under `contracts/godot-integration/v1/` differs from HEAD.
Since the freeze, two commits touched `contracts/`, none a schema:

| Commit | Change |
|---|---|
| `4bc5259` (AS-07a) | added `fixtures/vectors/canonical-json-v1.json` and its `fixtures/INDEX.json` entry (test vectors) |
| `f0a178f` | `README.md`: documents the additive `representations` map |

Unchanged since the freeze: all `*.schema.json`, `capabilities.json`, `error-codes.json`, `integration-api.openapi.json`
(guarded by `tests/unit/test_openapi_fresh.py`), `static-source-package.md` (apart from the 2026-10-03 clarification in its §3 and changelog).

## Behaviour added since the freeze (all additive)

| Area | Change | Compatibility |
|---|---|---|
| `POST /resolve` | each entry carries a `representations` map keyed by the requested representation id (`state`, `error`) next to the existing `state`/`deliveries` fields | additive; clients that ignore it behave as before. The addon honours it and falls back to `unsupported_representation` |
| Capability enforcement | the addon refuses a manifest (root or dependency) whose `required_capabilities` it does not support (`unsupported_contract`); `SUPPORTED_CAPABILITIES` is drift-tested against `capabilities.json` | client-side tightening only; no server or schema change. Older addons that ignored unknown capabilities could install assets they cannot use |
| Source install | `godot_static_source_v1` deliveries install, restore and verify (`add --representation`, `--trust-shaders`); the package validator and the publisher share one policy module (`project/as_srcpkg_policy.gd`), drift-tested against `capabilities.json` | uses the frozen `static-source-package.schema.json`/`.md`; lock and receipt formats for portable deliveries unchanged |
| Source publish | `publish` (scene -> new asset / new version) and the server publication endpoints | uses the frozen `publication-descriptor-draft.schema.json`; explicit commit with compare-and-swap |
| Source scene types | `[ext_resource type="Material"]` (Godot's save class of an external material; AS-11 acceptance) is accepted for `ext_resource` only, in the server validator (`source_scene.py`) and the addon (`EXT_SAVE_CLASSES` in `as_srcpkg_policy.gd`). The referenced `.tres` header must still be in `allowed_resource_types`; `capabilities.json` is unchanged; `static-source-package.md` §3 documents the exception (v1 clarification 2026-10-03) | client and server both relaxed; an older server refuses such scenes with `type_not_allowed`, an older addon refuses them with `unsupported_source` |
| Source text format 4 | text `format=4` is accepted next to `format=3` in the server (`godot_text.py`) and addon (`as_godot_text.gd`, `as_source_text.gd`) parsers; Godot 4.7.2 writes it for scenes embedding an `ArrayMesh` (base64 `PackedByteArray("...")` values, which the parsers already handled). Other formats stay `unsupported_format`. Documented in `static-source-package.md` §3 and its changelog (v1 clarification 2026-10-03, together with the `Material` exception above); fixtures `valid/array_mesh_prop`, `hostile/unsupported_text_format` | client and server both relaxed; an older server or addon refuses such scenes with `unsupported_format`. No schema, `capabilities.json` or `schema_version` change |
| Export | `export-preflight` command, `scripts/godot_export_wrapper.py`, `assetstudio-addon-<v>.manifest.json` | new CLI surface only; existing commands and exit codes unchanged |
| Web UI | read-only delivery/source indicators on asset detail | derived from existing `shown_version.artifacts` roles; no API change |

## Compatibility notes for consumers

- Locks written by 0.2.0 stay valid (schema unchanged); a lock that binds a `godot_static_source_v1` delivery needs an addon with source install (this tree).
- The addon version bumped 0.2.0 -> 0.2.1 only (no contract bump). `dist/assetstudio-addon-0.2.0.zip` is stale; rebuild.
- Exports of a project with `portable_glb_v1` only are unaffected; `export-preflight` is opt-in (the wrapper uses it).

## Risks and follow-ups

- Preset checks use fixed sample paths and Godot-style glob matching; exotic presets (selective export lists) are not modelled.
- Reference scan is textual (`res://addons/assetstudio/...` in `.tscn`/`.tres`/`project.godot`), not a full dependency walk.
