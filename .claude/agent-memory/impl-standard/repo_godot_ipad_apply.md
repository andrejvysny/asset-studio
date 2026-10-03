---
name: godot-ipad-apply
description: godot-ipad IP-07 Apply/bake/verify gotchas (Terrain3D, coordinator, tests, CLI)
metadata:
  type: project
---

Repo /Users/andrejvysny/workspace/godot/godot-ipad (not asset-studio). Code: app/addons/world_painter/{apply,runtime,cli}, ADR 0017.

- Run tests only via `python3 scripts/godot_test.py --sandbox wp --suite integration --filter X < /dev/null` (copies app/ to build/test_sandboxes; tests may write res:// there). `--filter test_` runs everything (minutes). No `timeout` binary on macOS: use `perl -e 'alarm N; exec @ARGV'`.
- New class_name files need `godot --headless --path app --import` once before ad hoc `--script` runs (sandbox runs import themselves).
- Terrain3D is unusable before its tree is ready (`data` null inside a SceneTree script's `_initialize`; CLI commands run there): write regions with `Terrain3DRegion.save` + `data_directory` string, never `Terrain3DData.save_directory`; load back via ResourceLoader. File names: `_%02d` for x>=0 else `%03d`.
- Headless `set_shader_param` stores nothing; set `Terrain3DMaterial._shader_parameters` dict directly. Textures must be file-backed (`resource_path` + ResourceSaver) or Terrain3D logs warnings that fail tests; Terrain3D tests need ApplyTestCase (tolerates the one known deprecation warning).
- Set `res.resource_path = path` before ResourceSaver.save to get ext_resource refs (FLAG_CHANGE_PATH from script did nothing). Staged text resources get their paths rewritten (ApplyStager.normalize); binary .res must not reference staged paths.
- AssetStudio coordinator: `fail_after_step` static for crash tests; ops are project-relative; rollback of rename_dir deletes the staged dir; lock owner ids are slugs (no "/", <=64).
- `FileAccess.open(p, WRITE).store_buffer(x)` as a temp may not flush before the next read; keep a var and close().
- Contract fixtures: generator scripts/generate_world_v4_fixtures.py regenerates INDEX/vectors; `kind: vectors` entries are skipped by world loops.
