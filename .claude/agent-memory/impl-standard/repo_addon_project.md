---
name: repo-addon-project
description: AS-07a addon project modules (coordinator/lock/installer/CLI) gotchas
metadata:
  type: project
---

- Code: addons/assetstudio/{cli.gd,project/*,core/as_canonical_json.gd}; tests tests/test_{canonical_json,project_model,coordinator,installer}.gd + project_test_base.gd (helper, not run) + run_consumer_tests.py (E2E, needs godot + python3).
- Godot strings cannot hold U+0000 (becomes U+FFFD); JSON "\u0000" does not round-trip; GDScript vector test skips that case.
- `OS.is_process_running` only works for child processes (errors otherwise): stale-mutex pid check uses `kill -0` via OS.execute.
- macOS `sed -i` fails in `&&` chains and silently skips later commands; use python for edits. Python scripts need `uv run` (system python lacks trimesh/assetstudio_core).
- Generators: run make_integration_vectors.py BEFORE make_integration_fixtures.py (INDEX includes vectors file hash).
- E2E consumer uses custom_user_dir_name to isolate user:// registry/cache; runner deletes it from ~/Library/Application Support or XDG data dir.
