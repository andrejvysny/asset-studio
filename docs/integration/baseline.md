# Godot integration — AS-00 baseline (2026-10-01)

Spec: AS-SPEC-1.0 + INT-SPEC-1.0. Checkout `master` at `a2a657b86558e2e6047181e50f7759a3f116ca99` (matches the spec baseline), clean tree.
Toolchain: macOS arm64, Python 3.12 (arm64 venv), `uv` 0.11, Godot `4.7.2.stable.official.ed1daf0bf`.

## Test baseline

| Command | Result |
|---|---|
| `make lint` | PASS |
| `uv run pytest -q` | 447 passed, 2 failed (pre-existing, unrelated to integration) |

Pre-existing failures, left untouched:

- `tests/unit/test_runtime_contracts.py::test_repo_lock_marks_dinov3_pending`: DINOv3 weights are not downloaded on this machine (`missing: model.safetensors`). Environment-dependent.
- `tests/regression/test_task_ownership.py::test_gpu_acquire_failures_are_visible_then_bounded`: deterministic failure. `coord.choose("gpu1")` returns the task inside the 20 s backoff window.

Environment note: the previous `.venv` used an x86_64 interpreter, but `uv.lock` pins only arm64 macOS wheels for `cryptography`, so the source build failed. The venv was recreated with the arm64 CPython 3.12.

## Listener and port audit

| Listener | Bind (host) | Auth | Notes |
|---|---|---|---|
| Product UI + REST `:8190` | `STUDIO_HOST` (default `127.0.0.1`); compose maps `127.0.0.1:8190` | none; mutating `/api/` needs `x-assetstudio: 1` + same origin | Must never be LAN-exposed by the integration work |
| MCP `:8191` | `STUDIO_MCP_HOST`; compose `${MCP_BIND:-127.0.0.1}` | bearer `ast_` tokens, `read`/`full`, no project scoping | Separate `_CompanionServer` in the same process (`main.py:118-136`) |
| ComfyUI `:8188` | compose `127.0.0.1` | none | Inference |
| aux, worker3d | internal network only | none | No host port |

The Dockerfile sets `STUDIO_HOST=0.0.0.0` and `STUDIO_MCP_HOST=0.0.0.0` inside the container. Host exposure is controlled only by the compose port mapping. A new integration listener on `:8192` follows the same pattern: its own FastAPI app, so it does not inherit product routes or CSRF middleware.

## Reusable pieces

- **Role checks:** `packages/assetstudio_core/contracts.py`. model3d requires `model` and allows `preview` and `meta`. Enforced only in `publication.publish()`.
- **Token helpers:** `mcp_api/auth.py`. Covers sha256-hashed storage, `_write_private` (0600, fsync, replace), mtime reload, `hmac.compare_digest`, and the raw ASGI `BearerAuth`.
- **No persistent server identity.** `Settings.instance_id` is hostname plus random per start. The instance directory is not part of project backups.
- **Event bus:** in-memory ring of 5000 events. The epoch is whole-second time, and events carry only `project_id`.

## Godot spikes

| Spike | Result |
|---|---|
| Headless import-ready transition | PASS. A freshly materialized `prop.glb` is **not loadable** before import (`No loader found`). After `godot --headless --editor --path . --import` (~1.2 s), it loads as `PackedScene` and instantiates. A restore must import before use, and `update_file()` alone is insufficient. |
| Dock to 3D viewport GUI drag | **NOT RUN**. Deferred to AS-08 by decision. It needs a real editor session. |

Harness project: `integrations/godot/project.godot`. The addon lives under the harness root because a Godot project cannot reference files outside `res://`. This deviates from the proposed `integrations/godot/tests/project.godot` path.
