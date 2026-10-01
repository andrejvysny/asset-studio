# Handoff: AS-00 → AS-05 (2026-10-01)

| Field | Value |
|---|---|
| Repository / branch / commit | `asset-studio` / `master` / `a2a657b` plus an uncommitted working tree (nothing committed or pushed) |
| Spec | AS-SPEC-1.0 + INT-SPEC-1.0. Deviation: descriptor `forward_axis = "+Z"` ([ADR 0001](../adr/0001-descriptor-forward-axis.md)) |
| Completed tasks | AS-00, AS-01, AS-02, AS-03, AS-04, AS-05 (backend scope) |
| Next dependency-ready task | **AS-06** GDScript runtime client and cache, built on `integrations/godot/addons/assetstudio/core/as_canonical.gd` |

## Interfaces produced

- **Contract bundle** `contracts/godot-integration/v1/`:
  - schemas: asset-ref, asset-descriptor, delivery-manifest, static-source-package, project-lock, error, publication-descriptor-draft
  - `capabilities.json`, `error-codes.json`, `static-source-package.md` grammar
  - generated `integration-api.openapi.json`
  - fixtures with `INDEX.json`: 7 valid and 18 hostile source packages, plus descriptors, manifests, locks, drafts and golden vectors
- **Versions:** API 1, contract 1, source package 1, asset-locks subformat 1. Delivery profiles are `legacy_glb_projection@1` and `published_descriptor@1`.
- **Python:**
  - `assetstudio_core.canonical_v1`, `.delivery`, `.source_manifest`, `.project_lock`, `.publication_draft`
  - `assetstudio_processing.source_package` (static validator), `.godot_text`, `.glb_budget`
  - `assetstudio_storage.delivery`
  - `assetstudio_server.integration_api` (listener) and the `services.deliveries` / `services.source_publications` service modules
- **Listener:** `STUDIO_INTEGRATION_*` settings and the `assetstudio integration token|identity` CLI. Details are in [api.md](api.md).
- **GDScript:** `as_canonical.gd` covers the asset key, raw sha256 and decimal parsing. A headless runner is at `integrations/godot/tests/run_tests.gd`.

## Commands and results

| Command | Result |
|---|---|
| `make lint` | PASS |
| `uv run pytest -q` | 653 passed, 3 failed. All 3 failures are pre-existing: DINOv3 weights are absent; the GPU backoff test fails on the baseline too; and the grouped-3D test is flaky, failing 3 of 8 runs on a clean baseline worktree |
| `scripts/make_integration_fixtures.py --check`, `make_integration_vectors.py --check`, `export_integration_openapi.py --check` | 0 |
| `godot --headless --path integrations/godot --script res://tests/run_tests.gd` (Godot 4.7.2) | 4/4 PASS. Python and GDScript agree on asset keys and raw hashes |
| Real-server smoke (`STUDIO_ENGINE=none`, integration on loopback) | PASS: health; product routes absent on the integration port; capabilities; import → resolve → manifest and GLB hashes exact; Range 206; `delivery_ready` event; exact missing version reports `version_unavailable`; read token refused publish; no token appeared in the server log |

## Known issues / NOT RUN

- **NOT RUN:**
  - GUI dock → 3D viewport drag. Deferred to AS-08. The headless import-ready spike passed (see [baseline.md](baseline.md)).
  - Docker image build including the new `COPY contracts/`.
  - Real GPU stack. It is not needed, because no GPU path changed.
  - LAN/TLS listener with a real certificate.
- **Memory:** preview validation reads the portable GLB into memory, bounded at 512 MiB. Starlette spools uploads before they are copied into staging.
- **Dependency check:** during preview it may prepare (write) derived descriptors and deliveries in the dependency's library. These writes are idempotent and immutable.
- **Fixture bytes:** trimesh-exported GLB fixture bytes depend on the pinned trimesh and numpy versions. ZIP and PNG fixture bytes are now independent of the zlib build.
- **Shared spec:** INT-SPEC §4.2 still says `-Z` and needs amending to match ADR 0001.
- **Agent memory:** `.claude/agent-memory/` was written by the implementation subagents. Delete it or ignore it before committing.

## Dependencies now available to IP/FG

- Frozen v1 contracts and fixtures, consumed by godot-ipad (IP-01/IP-02) and Fantasy-game.
- A working read, resolve and download API for exact references.
- Publication of static sources.
- The long-poll change feed.
- GDScript hashing primitives.
