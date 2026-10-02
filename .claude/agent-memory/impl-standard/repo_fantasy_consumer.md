---
name: repo-fantasy-consumer
description: fantasy-game consumer of the addon: CLI gotchas
metadata:
  type: project
---

- Binding ids must be lowercase slugs (`^[a-z0-9][a-z0-9_.-]{0,63}$`); uppercase asset names (pine_open_A) fail with "--binding must be a slug".
- Wrapper nests the GLB's own StaticBody3D (e.g. legacy spruce TrunkCollision); game must drop it if it owns collision.
- Patch rules on textured props reference delivery PNGs via ext_resource (no embedded data), contrary to README note.
- Descriptors are in user://assetstudio/cache/descriptors/*.json (slot ids/roles) after `add`.
- zsh: do not store godot cmd in a var with args; use a script wrapper with subprocess timeout + stdin=/dev/null.
