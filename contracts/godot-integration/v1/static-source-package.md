# GodotStaticSourcePackageV1 (source package version 1)

Normative grammar for the editable-source representation `godot_static_source_v1`. The package is **inert data**:
validation is static and never loads, instantiates, or executes anything. The server validator is AS-04
(`assetstudio_server`, not part of AS-01); it must reject every fixture in `fixtures/source_packages/hostile/` with the
error code and detail recorded in `fixtures/INDEX.json`. Godot-side consumers repeat the same checks before import.
Machine-readable allowlists and limits: `capabilities.json` (`source_package`, `limits`). Manifest shape:
`static-source-package.schema.json`.

Keywords MUST, MUST NOT, MAY are normative.

## 1. ZIP container

1. Compression method MUST be stored (0) or deflate (8). Any other method is rejected.
2. Encrypted members (general-purpose flag bit 0) MUST be rejected. Archives with data descriptors, multi-disk, or ZIP64
   inconsistencies MAY be rejected; trailing data after the end-of-central-directory record MUST be rejected.
3. Symlinks, devices and other non-regular members MUST be rejected (`external_attr >> 16` file type other than regular/0).
   Directory entries (names ending in `/`) are ignored and MUST NOT carry content; every other member is a regular file.
4. Member names: UTF-8, Unicode NFC, relative, `/` separators. MUST be rejected: absolute names (leading `/`, drive letters),
   any `..` or `.` segment, backslashes, empty segments, NUL or control characters, a name that is not NFC. Package paths
   use only `[A-Za-z0-9_.-]` segments (the `safe_path` pattern: max 255 chars, max 32 segments).
5. Two members whose names are equal after Unicode case folding (`str.casefold()` of the NFC name) MUST be rejected,
   even though they differ byte-wise.
6. Limits (INT §12): at most 4096 files, depth 32, 1 GiB expanded total and per member bound by that total; the upload is
   at most 512 MiB. Per-member compression ratio (expanded / compressed) above 200 MUST be rejected (`resource_limit`).
   Sizes MUST be enforced while streaming, not trusted from headers.
7. Member set is exact: `source_manifest.json` plus exactly the paths in `files[]`. A missing declared file, or an
   undeclared member, MUST be rejected. `source_manifest.json` MUST NOT list itself. Every member's SHA-256 and byte size
   MUST equal its `files[]` entry (mismatch is `integrity_mismatch`).
8. Allowed file extensions: `.tscn .tres .gdshader .gdshaderinc .png .jpg .jpeg .webp .glb`. Forbidden:
   `.gd .cs .gdextension .pck .so .dylib .dll .exe .res .scn` and any member named `project.godot`. Anything else,
   including `.import`, `.godot/` content, autoloads and plugins, is rejected. The one JSON member is `source_manifest.json`.

## 2. `source_manifest.json`

Canonical JSON (sorted keys, no floats; decimals are strings). Top-level names, all mandatory and no others:

| Name | Meaning |
|---|---|
| `schema_version` | `1` |
| `entry_scene` | Safe path of a `.tscn` listed in `files` |
| `source_godot_version` | Godot version that authored the source, e.g. `4.4.1-stable` (informational) |
| `files` | `[{path, sha256, size, media_type}]`, 1..4096, unique paths including case-fold |
| `resource_map` | `res://` reference -> `package_file` or `asset_dependency` entry (below) |
| `asset_dependencies` | asset_key -> `{asset_ref, descriptor_sha256, representation, delivery_id\|null}` |
| `capabilities` | Subset of `known_capabilities`, no duplicates |
| `conversion_report` | `{portable_status: exact\|approximated\|desktop_only, omissions: [str], approximations: [{slot_id, reason}]}` |
| `placement` | `{placement_anchor, footprint_radius_m, scale_range, height_offset_range_m, default_grounding, material_slots: [{slot_id, role, source_surfaces: [{node_path, surface}]}]}`; same values and ids as the descriptor |

`resource_map` keys are original reference strings exactly as written in the source: they start with `res://`, contain no
`..` segment, no backslash, no control characters. Values:

```json
{"kind": "package_file", "path": "materials/prop_mat.tres", "original_uid": "uid://b3k1m0pa1n7ro"}
{"kind": "asset_dependency", "asset_key": "<64 hex>", "entrypoint": "portable.glb", "original_uid": null}
```

- `package_file.path` MUST be listed in `files`. The map MUST cover every `res://` reference in the package: every
  `ext_resource` path and every shader `#include`. A reference without an entry is rejected as an unsafe or unavailable
  dependency. No URL, `user://`, `uid://`-only, absolute filesystem or `file://` reference is ever mapped.
- `asset_dependency.asset_key` MUST be a key of `asset_dependencies`, whose key MUST equal the asset key recomputed from
  its `asset_ref` (INT §4.4). A dependency naming a key that is absent, or unavailable or unauthorized for the caller, is
  `unsupported_source_dependency`. `entrypoint` is a safe path inside that dependency's delivery (`portable.glb` for a
  portable dependency).
- `original_uid` records the source's `uid://` and is **never trusted** for resolution; resolution is by mapped path.
  The installer removes or remaps foreign UIDs and the local path fallback must stay valid.
- `placement.material_slots[].source_surfaces` MUST equal the descriptor's `godot_static_source_v1` surfaces. `node_path`
  is relative to the scene root: `.` for the root, otherwise `/`-joined node names of `[A-Za-z0-9_-]`.

## 3. Godot text format subset (`.tscn`, `.tres`)

Parsing is a bounded, line-oriented static parse; no `eval`, `str_to_var`, or class instantiation. Text format 3 only
(`format=3`); binary `.res` / `.scn` and any file that does not start with a text header are rejected.

- Allowed section headers: `gd_scene`, `gd_resource`, `ext_resource`, `sub_resource`, `node`, `resource`, `editable`.
- Forbidden: `connection` (any signal wiring), and any other header.
- Forbidden properties: any `script` property (`script = ...`), `metadata/_custom_type_script`, and any property whose value
  references a Script resource.
- `ext_resource` `type` MUST NOT be `Script`, `GDScript`, `CSharpScript` or `GDExtension`, and MUST be in
  `allowed_resource_types`. `sub_resource` `type` and the `.tres` `gd_resource` `type` MUST be in `allowed_resource_types`.
- `node` `type` MUST be in `allowed_node_types`. A node without `type` MUST carry `instance=ExtResource(...)` (a nested
  scene or imported `.glb`); custom classes, even if named like a built-in, are rejected.
- `ext_resource` `path` MUST be a key of `resource_map`. `uid` is recorded, never used to resolve.
- Anything with animation, skin, particle, or VFX content (`AnimationPlayer`, `Skeleton3D`, `GPUParticles3D`, ...) is not in
  the allowlist and is rejected, not dropped.
- Property values are parsed as inert data only (numbers, strings, `Vector3`, `Color`, `Transform3D`, `ExtResource`,
  `SubResource`). A value that calls a constructor not in this list is rejected.

## 4. Shaders (`.gdshader`, `.gdshaderinc`)

- Source-only capability `shader_source`; a desktop trust warning is required and shaders are not executed on iPad.
- `#include` MAY only name `res://` paths that are `package_file` entries. A `..` segment, other scheme, or unmapped path is
  rejected. Static validation does not prove a shader is GPU-safe; do not claim it does.
- A `ShaderMaterial` portable projection needs a disclosed approximation (`conversion_report`, descriptor warning
  `custom_shader_approximated`) or the asset is `desktop_only`.

## 5. Packaged `.glb`

Must be self-contained: no `uri` on any `buffers[]` or `images[]` entry other than an embedded data URI or the GLB BIN chunk;
no unsupported `extensionsRequired`; no skins (`skins`) and no animations (`animations`). A `.glb` extension alone proves
nothing: parse the JSON chunk. The iPad structural budgets in `capabilities.json` apply to the portable representation only; the source package is
bound by the ZIP limits in §1.

## 6. Preview upload and delivery

Preview upload is `multipart/form-data` with parts: `source` (the source zip), `portable` (`portable.glb`), `descriptor`
(descriptor draft JSON), `thumbnail` (PNG, optional), `report` (`conversion_report.json`, required with `source`). Only
`source` is a package; the other parts are declared by the upload, are not zip members, and are re-validated by the
server (the draft against `publication-descriptor-draft.schema.json`, GLB against §5 with iPad budgets from
`capabilities.json` reported, not enforced). The server computes bounds, asset reference and provenance itself and builds
the final `AssetDescriptorV1` at commit. It rechecks every claim; nothing in the upload is trusted.

A published `godot_static_source_v1` delivery manifest has one file, `source.zip` (the original archive, unchanged),
which is its entrypoint; its `dependencies[]` mirror `asset_dependencies` with exact delivery ids and manifest hashes.
Installation preserves the archive and writes a derived, relocated tree by rewriting only mapped references with a tested
parser/serializer, never global text replacement.

## 7. Error mapping

| Violation | Code |
|---|---|
| Any rule in §1-§5 about unsafe content, names, structure, undeclared members | `unsafe_package` |
| Size, file count, depth, expansion, ratio | `resource_limit` |
| Declared hash or size differs from bytes | `integrity_mismatch` |
| Missing, unauthorized, or unmapped asset dependency | `unsupported_source_dependency` |
| Schema-invalid manifest or upload | `invalid_request` |

The expected code and a short detail slug for each hostile fixture are in `fixtures/INDEX.json` (`expected`, `detail`).
