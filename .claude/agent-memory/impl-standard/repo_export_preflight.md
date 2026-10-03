---
name: repo-export-preflight
description: AS-10 export preflight/wrapper/packaging facts for asset-studio Godot addon
metadata:
  type: project
---

- One policy module: `project/as_srcpkg_policy.gd` (installer + publisher); `as_source_text.gd` is a thin scan over `as_godot_text.gd`. as_source_policy.gd is gone.
- `export-preflight` = `project/as_export_preflight.gd` + `as_export_presets.gd`; real "imported" evidence is a `[remap]` in `<file>.import` plus the `.godot/imported` file (installer pre-seed only has `[params]`).
- Wrapper `scripts/godot_export_wrapper.py`; E2E `integrations/godot/tests/run_export_tests.py` (imports run_consumer_tests as module; macOS export needs `rendering/textures/vram_compression/import_etc2_astc=true`; templates in ~/Library/Application Support/Godot/export_templates/<ver>/).
- Web has no test/lint runner: only `npx tsc -b --noEmit`. `make lint` does not cover scripts/ or integrations/ python.
