# Blender integration (Phases 3–4)

Pinned toolchain: Blender 5.2.2 LTS. Planned layout:

- `addon/` — bridge add-on (version-pinned from mcp-for-blender), serialized main-thread mutations checked against
  the workspace generation.
- `inspection/` — restricted source inspection: readability, dependency inventory, entry point.
- `export/` — pinned GLB export profiles.
- `validation/` — geometry, material, rig and animation profile checks.

Not implemented yet; see `Modular_system_spec.html` §9, §15 and §18.
