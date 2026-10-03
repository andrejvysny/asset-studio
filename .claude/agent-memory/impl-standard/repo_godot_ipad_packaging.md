---
name: repo-godot-ipad-packaging
description: godot-ipad IP-09 packaging/CLI/minimal-consumer gotchas (separate repo from asset-studio)
metadata:
  type: project
---

- godot-ipad: scripts/package_world_painter.py + install_world_painter.py + make_minimal_consumer.py share scripts/wp_archive.py; tests scripts/tests/test_world_painter_{packaging,cli}.py, test_minimal_consumer.py.
- macOS has no `timeout`; run Godot via python subprocess (timeout, stdin DEVNULL). zsh aliases `g` = git, so don't name shell functions `g`.
- Zip entries written with external_attr 0o644<<16 have no S_IFREG bit; installer must accept file-type 0.
- Godot import writes .uid files for scripts the archive lacks (AssetStudio ships none): --check ignores .uid/.import extras.
- Catalog hash (Python worldpoc_values + GDScript) covers only catalog.json + preview_scene/scatter_mesh files; thumbnails are not hashed or read.
- `rm -rf` is denied by the sandbox permission; use unique temp dirs under the scratchpad instead of reusing old ones.
