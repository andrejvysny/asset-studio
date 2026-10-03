---
name: repo-publisher
description: AS-09 Godot publisher gotchas (collector/export/zip/journal, fake server, test runners)
metadata:
  type: project
---

- Packed arrays stored in a Dictionary and appended via `(d["k"] as PackedStringArray).append()` are copies: use plain `Array` for problems/warnings lists.
- GDScript regex `^` needs `(?m)` for multi-line text (shader `#include` scan); `Mesh.surface_get_format` exists only on ArrayMesh.
- ZIPPacker stamps current time + adds dir entries: as_source_writer builds the zip itself (gzip codec gives raw deflate + CRC-32; fixed 1980 timestamp).
- GLTF export: CSG shapes are exported unbaked unless replaced by `bake_static_mesh()` meshes (needs 2 frames in a SceneTree); StaticBody/CollisionShape export OMI_physics extensions, so they are stripped; mesh/primitive indices are found by unique node names in the GLB JSON.
- Server checks reusable offline: tests/publish_check.py (`uv run python`) calls assetstudio_server...source_publication_checks; real-listener E2E is tests/contract/test_godot_publisher_real_server.py (uvicorn thread + godot CLI).
- Journal `.assetstudio/publish/journal.json` stores the receipt as a JSON string (canonical writer rejects non-integral floats); identical publish intents are answered from it, `--fresh` bypasses.
- fake_server publication code lives in tests/fake_publication.py (mixin); control: /__publications, /__publish_reset; scenarios preview_busy, drop_commit_response.
- Godot warning backtraces ("invalid UID ... using text path") print GDScript frames in stderr: they are not script errors.
