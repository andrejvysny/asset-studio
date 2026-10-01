# Godot integration API v1

This is AssetStudio's authenticated delivery and publication facade for Godot consumers: the AssetStudio Godot addon, godot-ipad and Fantasy-game. It implements INT-SPEC-1.0 §5–§8 and AS-SPEC-1.0 §3.

The frozen contracts live in [`contracts/godot-integration/v1/`](../../contracts/godot-integration/v1/README.md). The machine-readable route list is `contracts/godot-integration/v1/integration-api.openapi.json`, which is generated from the app and checked by a test.

## Listener

| Setting | Default | Meaning |
|---|---|---|
| `STUDIO_INTEGRATION_ENABLED` | `0` | Starts a separate listener inside the Studio process. It mounts only `/api/integration/v1` and does not use a new database or worker. |
| `STUDIO_INTEGRATION_HOST` | `127.0.0.1` | Bind address. A non-loopback bind is refused (exit 2) unless TLS or the insecure flag is set. |
| `STUDIO_INTEGRATION_PORT` | `8192` | |
| `STUDIO_INTEGRATION_TLS_CERT` / `_KEY` | empty | Enables TLS on the listener through uvicorn. |
| `STUDIO_INTEGRATION_ALLOW_INSECURE_LAN` | `0` | Allows cleartext on the LAN for development and logs a warning. Tokens sent in cleartext can be intercepted. |

With Docker, the container listens on `0.0.0.0` and host exposure is set by the compose mapping `${INTEGRATION_BIND:-127.0.0.1}:8192`. The product UI and API on `:8190` stay loopback-only and unauthenticated. Enabling integration never exposes them, and integration tokens never authorize them. MCP tokens are not accepted on this listener.

## Identity and credentials

- **Server identity:** `<instance>/integration/server.json` holds a UUID created once and never regenerated. Show it with `assetstudio integration identity show`. `identity adopt <uuid> --i-understand-fork` is only for moving the same server. Never use it to fork one.
- **Library:** one library is one AssetStudio project (`prj_…`).
- **Tokens:**
  - Create with `assetstudio integration token create <name> --library <prj> [--library …] [--scope assets:read|assets:publish]`. The plaintext token is printed once.
  - Manage with `token list` and `token revoke <name>`. Revocation is a tombstone and takes effect on the next request.
  - The token file stores only sha256 hashes, with mode 0600.
- **Request header:** `Authorization: Bearer asi_…`. Auth runs before the request body is read. A token never appears in a URL.
- **Publishing:** recorded with actor `integration:<token name>`.

## Errors

`{"error": {"code", "message", "retryable", "details"}}`. Codes are listed in `contracts/godot-integration/v1/error-codes.json`. An ungranted library and a nonexistent library return the same `403 forbidden`, so existence is never disclosed.

## Routes (prefix `/api/integration/v1`)

| Route | Scope | Notes |
|---|---|---|
| `GET /health` | none | Returns `{"status","service","api_version"}` only |
| `GET /capabilities` | any | `server_id`, versions, limits, representations, and granted libraries and scopes |
| `GET /libraries` | any | One entry per granted library. An unavailable library does not hide the others |
| `GET /libraries/{L}/assets` | read | Published `model3d` only. Accepts `q`, `category`, `tags` (comma-separated, AND), `cursor`, `limit` (default 60, max 200). A stale cursor returns `reset_required` |
| `GET /libraries/{L}/assets/{A}` | read | Current pointer, `metadata_revision`, versions |
| `GET /libraries/{L}/assets/{A}/versions/{V}` | read | The exact version only, never the current one. Returns descriptor state with exact text, delivery summaries and budget. Pure read |
| `GET …/versions/{V}/descriptor` | read | Raw descriptor bytes with `ETag`/`X-Content-SHA256`. Returns `409 delivery_preparing` until resolved |
| `GET …/versions/{V}/thumbnail` | read | Preview image |
| `POST /libraries/{L}/resolve` | read | Takes up to 200 exact `AssetRef`s and returns a state per entry: `ready`, `unsupported`, `not_found`, `forbidden`, `server_identity_mismatch` or `temporarily_unavailable`. Prepares legacy projections once, on CPU only. Idempotent |
| `GET /libraries/{L}/deliveries/{D}/manifest` | read | Raw immutable manifest bytes |
| `GET /libraries/{L}/artifacts/{R}/content` | read | Only artifacts referenced by a delivery of `L`. Verified before serving. `Range` returns 206 |
| `GET /changes?cursor=&timeout_s=0..20` | any | Long poll. Events: `library_changed`, `asset_metadata_changed`, `asset_current_changed`, `delivery_ready`. A foreign or expired cursor returns `reset_required`. `access_changed` is reserved and unused in v1 |
| `POST /libraries/{L}/publications:preview` | publish | Multipart upload (`portable`, `descriptor` draft, optional `source` + `report`, `thumbnail`). Validates and returns a receipt valid for 24 h. Never mutates the library |
| `POST /libraries/{L}/publications:commit` | publish | Atomic, idempotent, compare-and-swap on `expected_current_version` |
| `GET /libraries/{L}/publication-operations/{K}` | publish | Recovers a commit outcome by idempotency key |

## Immutability rules

- **Descriptor:** frozen once per version at `descriptors/<ast>/<ver>.json`, and the first freeze wins. A legacy GLB version receives a deterministic projection under profile `legacy_glb_projection@1`. Placement is bottom-centre, `scale_range` is 0.25–4 and `height_offset_range_m` is ±2 m.
- **Delivery IDs:** derived from version, representation and profile. The same ID with different bytes is `integrity_mismatch`. A bump to the projection code requires a new `profile_version`.
- **Stored records:** published `versions/` and `manifests/` records are never rewritten by integration reads.
- **Forward axis:** `forward_axis` is `+Z`, matching glTF and Godot `MODEL_FRONT`. See [ADR 0001](../adr/0001-descriptor-forward-axis.md).
