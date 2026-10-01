---
name: repo-integration-listener
description: Gotchas for asset-studio integration listener and test commands
metadata:
  type: project
---

- Integration listener lives in services/studio/assetstudio_server/integration_api/; tests use tests/integration_support.py; contracts read from contracts/godot-integration/v1 (STUDIO_CONTRACTS_DIR).
- Body-limit abort uses a BaseException subclass so FastAPI `except Exception` handlers do not turn it into 503.
- tests/contract/test_jobs_batches.py::test_grouped_3d_builds_do_not_thrash_gpu1... is flaky (passes on rerun); not a regression.
- zsh: use `pipestatus`, not PIPESTATUS; `--include` globs in grep need quoting.
- EventBus epoch is `<ns hex>-<rand>`; integration `/changes` cursor = b64url JSON {e,s}; library events carry asset_id+change (published/current/metadata). Api test helper has no .patch: use `api.raw("PATCH", ...)`; asset detail revision is `detail["manifest"]["revision"]`; routes are /api/v1/projects/{id}/assets/... (no /library).
- BSD sed here: `sed -i` fails; use python for edits.
- Descriptor/delivery storage: packages/assetstudio_storage/delivery.py; projection: services/studio/assetstudio_server/services/{delivery_projection,deliveries}.py; routes_assets.py. Keys descriptors/, deliveries/, delivery_index/, delivery_artifacts/ (first-write-wins).
- macOS sed needs `sed -i ''`; a failed sed in a chain is easy to miss — verify with grep. Other agents edit integration_api/app.py ROUTERS concurrently: re-read before editing.
- tests: `from x import *` skips underscore names; legacy fixture projects hold 1 model3d (60ff832) only.
