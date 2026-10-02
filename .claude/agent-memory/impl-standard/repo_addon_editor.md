---
name: repo-addon-editor
description: AS-08 addon editor/finalize/update gotchas
metadata:
  type: project
---

- All addon scripts (except cli.gd) are `@tool`: non-tool scripts are placeholders when instantiated in the editor (ASResult.new() etc. would break).
- project/ and core/ must not contain the word "Editor" (grep check), even in comments.
- Imported mesh resource_name is "<node>_<gltf mesh>", not the bare glTF name (slot resolver uses suffix match).
- Wrapper bytes: PackedScene.pack + ResourceSaver to user:// tmp, then strip unique_id/uid and renumber ext ids (as_wrapper.normalize_text) for determinism.
- Coordinator rejects two ops on the same path in one txn; use State.wrappers_bytes_multi. txn.summary is persisted into history.json (rollback uses it).
- Dock must be add_control_to_dock'd before setup() (awaits need get_tree()). Always wrap godot runs in python subprocess with timeout; run_plugin_tests.py takes ~2 min.
- tests: run_plugin_tests.py (editor_selftest test-only plugin driven by ASSETSTUDIO_SELFTEST=1), fake_server has /assets, /__current, /__events.
