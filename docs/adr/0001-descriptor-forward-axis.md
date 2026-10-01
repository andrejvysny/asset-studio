# ADR 0001: The asset descriptor forward axis is +Z

- **Status:** Accepted (2026-10-01)
- **Scope:** `AssetDescriptorV1.forward_axis` (contract `godot-integration/v1`, owned by AssetStudio)
- **Deviates from:** INT-SPEC-1.0 §4.2, which proposed "Metres, `+Y`, `-Z` in normalized deliveries"

## Context

INT §4.2 proposes `-Z` as the forward axis for normalized deliveries. That matches Godot's camera and node "forward" convention.

Model assets follow a different convention:

- The glTF 2.0 specification states that "the front of a glTF asset faces +Z".
- Godot 4 defines `Vector3.MODEL_FRONT = +Z`.
- The Godot glTF importer does not rotate models on import.
- The exporter (`GLTFDocument`) writes Godot coordinates unchanged.

INT §4.2 also forbids rotating models on load. Published versions are immutable, so existing GLBs cannot be rewritten to face `-Z`. This applies to both legacy imports and TRELLIS outputs. A `-Z` descriptor value would therefore be false for every existing GLB.

## Decision

`forward_axis` is the literal `"+Z"` in descriptor v1. This is the glTF and Godot model-front convention.

- `up_axis` stays `"+Y"` and `units` stays `"m"`.
- Consumers place assets by anchor, rotation and scale only. They never apply a corrective rotation on load.
- Any orientation normalization belongs to a reviewed publication step and is recorded in provenance.

## Consequences

- The value is truthful for legacy projections without touching immutable bytes.
- godot-ipad and Fantasy-game read `+Z` from the frozen schema (`contracts/godot-integration/v1/asset-descriptor.schema.json`). INT §4.2 should be amended to match. Until that happens, this ADR is the authoritative interpretation.
- A future contract version may add other values. v1 readers reject any other value as `unsupported_contract`.
