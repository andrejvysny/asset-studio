# AssetStudio x Godot integration contracts, v1

Frozen cross-repository contract (INT-SPEC-1.0 §4-§8, §12; AS-01). **AssetStudio is the canonical owner.** Other
repositories (Godot addon, World Painter, iPad runtime) consume this directory **unchanged** (copy or vendor it, never
edit the copy). A change means a new version directory, not an edit.

Versions: API 1, contract 1, source package 1 (also in `capabilities.json`).

## Files

| File | What it is |
|---|---|
| `asset-ref.schema.json` | `AssetRef` plus `$defs` shared by every other schema (ids, slugs, sha256, safe paths, canonical decimals, capability and representation enums) |
| `asset-descriptor.schema.json` | `AssetDescriptorV1` (immutable placement metadata) |
| `delivery-manifest.schema.json` | `DeliveryManifestV1` |
| `static-source-package.schema.json` | `source_manifest.json` inside a `GodotStaticSourcePackageV1` zip |
| `publication-descriptor-draft.schema.json` | `DescriptorDraftV1`: the placement fields a publisher declares in the `descriptor` part of `publications:preview` (the server computes bounds, asset ref, provenance) |
| `project-lock.schema.json` | `ProjectAssetLockV1` (`assetstudio.lock.json`) |
| `error.schema.json`, `error-codes.json` | Structured error envelope; code, HTTP status, retryable, meaning |
| `capabilities.json` | Versions, representations, known capabilities, source-package allowlists, resource limits |
| `static-source-package.md` | Normative grammar and ZIP rules for source packages |
| `fixtures/` | Deterministic valid and hostile fixtures; `fixtures/INDEX.json` lists every file with sha256 and expected outcome |

Schemas are JSON Schema draft 2020-12 with `additionalProperties: false` except free-form JSON values
(`source_provenance`, `licence`, error `details`; these must not contain floats). Schemas reference each other by relative
`$ref` (`asset-ref.schema.json#/$defs/...`) and have `$id`s under `https://schemas.assetstudio.invalid/godot-integration/v1/`;
resolve them from the local files, never from the network.

Typed Python models: `packages/assetstudio_core/delivery.py`, `source_manifest.py`, `project_lock.py`. Encodings
(canonical bytes, decimal strings, asset keys): `canonical_v1.py`; golden vectors: `fixtures/vectors/canonical-v1.json`.
A schema cannot express everything: ordering, uniqueness (including case-fold), key recomputation, closure and cycle
checks live in the models and are documented in each schema's `description`. Fixtures marked `semantic_invalid` in
INDEX.json pass the schema and fail the model on purpose; consumers must implement those checks too.

## Conventions

- Raw bytes are the contract: documents are canonical JSON (sorted keys, no whitespace, UTF-8, no floats/NaN). Clients
  hash the received bytes, never a re-serialization.
- Every non-integral number is a canonical decimal string (<= 6 fractional digits, no exponent, no trailing zeros, no `-0`).
- **Descriptor `forward_axis` is `"+Z"`** (glTF / Godot `MODEL_FRONT`). This deviates from INT-SPEC §4.2, which says `-Z`.
  See [ADR 0001](../../../docs/adr/0001-descriptor-forward-axis.md).
- A `godot_static_source_v1` delivery manifest lists one file, the unchanged source zip (`source.zip`), as its entrypoint.
  A `portable_glb_v1` delivery lists `portable.glb`.

## Resolve entries and admission errors

Informative (documentation only; no schema or wire change). The OpenAPI document does not type the resolve response.

Each entry of `POST /libraries/{L}/resolve` carries `asset_ref`, `asset_key`, `state`, `error` (`{code, message}` or
`null`), `descriptor_sha256`, `descriptor_json`, `deliveries`, `dependencies` and the additive `representations` map.
`representations` is keyed by each requested representation id (`portable_glb_v1`, `godot_static_source_v1`,
`mobile_glb_v1`); every value is `{"state": ..., "error": {"code", "message"} | null}`, where `state` is `ready`,
`unsupported`, `not_found`, `forbidden`, `server_identity_mismatch` or `temporarily_unavailable`. The entry `state` is `ready`
when at least one requested representation is ready, so one failed representation never hides a ready one.

Publication endpoints (`publications:preview`, commit) refuse admission with the `temporarily_unavailable` error
(HTTP 503, retryable). Its `details.reason` is one of:

| `details.reason` | Meaning |
|---|---|
| `staging_capacity` | The staging area is full; retry later |
| `disk_floor` | Free disk would drop below the configured floor; retry later |
| `processing_queue_full` | Too many publications are processing or queued; retry with backoff |
| `preview_quota` | The token already holds the maximum number of pending previews; commit them or let them expire |

Clients must treat an unknown `reason` as plain `temporarily_unavailable`.

## Publication commit idempotency

`POST /publications:commit` is bound to its `idempotency_key` (per library) and the publisher's credential id. The binding
covers every request field except `preview_id` and `idempotency_key`: `package_sha256`, `portable_sha256`,
`descriptor_draft_sha256`, `target_asset_id`, `expected_current_version`, `name`, `category_id`, `tags`, `licence`,
`source_uri`, `credit`. `preview_id` is only a transport handle for content already pinned by those sha256 values, so a
retry that re-uploaded identical content as a fresh preview (for example after losing the preview, or after a crash
between the server's intent record and its publish receipt) returns the original outcome; the first attempt's
`preview_id` stays the version's recorded provenance. Reusing the key with any other bound field, or from another
credential, is `idempotency_conflict`. No request or response schema changes.

## Regenerating fixtures

```sh
uv run python scripts/make_integration_vectors.py     # canonical-v1.json
uv run python scripts/make_integration_fixtures.py    # everything else, incl. INDEX.json
uv run python scripts/make_integration_fixtures.py --check   # exit 0 when committed files are current
```

Output is deterministic (fixed ids, 1980-01-01 zip timestamps, sorted members, deflate level 9, canonical JSON); GLB/PNG
bytes depend on the pinned trimesh/Pillow versions in `uv.lock`. Run `uv run pytest tests/unit/test_integration_contracts.py`
after any change.
