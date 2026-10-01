# fantasy-base library import

Bulk publication of the static GLBs of `fantasy-game-library` into one AssetStudio library through the integration publish API, with `scripts/import_library.py`.

| | |
|---|---|
| Library (project) | `fantasy-base`, id `prj_fs9ddwqfkjgytnsj`, root `~/AssetStudio/fantasy-base` |
| Server id | `75e29ae3-fe41-4ea9-99d1-8d8f3f88ee76` (instance `~/.local/share/assetstudio`) |
| Token | `fb-importer`, library-scoped, `assets:publish` + `assets:read`; plaintext in `~/.config/assetstudio/fantasy-base-importer.token` (0600, not in git) |
| State manifest | `~/.local/state/assetstudio-import/fantasy-base.json` (no secrets) |
| Run date | 2026-10-01 |

## Result

| Kind | Source | Published | Failed |
|---|---|---|---|
| nature | `assets/nature/{rocks,trees,groundcover,understory,debris}` | 24 | 0 |
| spruce | `spruce_trees/models` | 45 | 0 |
| prop | `props` | 3 | 0 |
| grass | `grass/glb` | 3 | 0 |
| character | `characters/character.glb` | 1 | 0 |
| **total** | | **76** | **0** |

- `--verify`: 76/76 resolve `ready` for `portable_glb_v1`; the state records `descriptor_sha256` and the portable delivery id per asset.
- Rerun: all 76 `skipped`. With the state file deleted, a rerun recovers every ref from `publication-operations/{key}` (identical refs, no new preview, no new version, library still lists 76 assets, all `display_version` 1).
- Warnings: none (every GLB is within the iPad budget; the largest is the 99k-triangle lodge/props and 97k-triangle character, limit 200k).
- Not published: `materials/`, shaders, `.blend`, previews, duplicate textures, `.import` files, `scripts/`, `tools/`.

## Mapping

- Descriptor: anchor `0 0 0` (pivot at ground contact), `FOLLOW_TERRAIN`, height offset `-0.5..0.5`, no collision. Footprint radius is `max(|x|,|z|)` of the bbox (nature json, else measured GLB), spruce `crown_radius_m`. Scale range is the nature json value, spruce `0.8..1.25`, others `0.5..2`.
- Material slots: one per glTF material used (`slot_id` = slug of the material name; `foliage` role when the name contains foliage/leaf/needle/grass, else `solid`). A primitive without material goes to a `default` slot.
- Commit: name = asset id (spruce: variant name), tags = kind, group, family, habitats, live/dead; licence `own work`, credit `fantasy-game procedural kit`, `source_uri` `fantasy-game-library/<relpath>`, idempotency key `fgl-<relpath-slug>-v1`.
- A changed source file under an existing key is recorded as `conflict`; the importer never bumps the key version by itself.

## Commands

```sh
# once: project + token (Studio stopped or running; token plaintext is printed once)
uv run assetstudio project create fantasy-base
uv run assetstudio integration token create fb-importer --library prj_fs9ddwqfkjgytnsj \
  --scope assets:publish --scope assets:read | grep '^asi_' > ~/.config/assetstudio/fantasy-base-importer.token
chmod 600 ~/.config/assetstudio/fantasy-base-importer.token

# Studio with the integration listener (no GPU engine)
STUDIO_ENGINE=none STUDIO_INTEGRATION_ENABLED=1 STUDIO_MCP=0 uv run assetstudio serve

ARGS="--source <fantasy-game-library> --library prj_fs9ddwqfkjgytnsj --token-file ~/.config/assetstudio/fantasy-base-importer.token"
uv run python scripts/import_library.py $ARGS --dry-run    # offline checks + table
uv run python scripts/import_library.py $ARGS              # publish (resumable)
uv run python scripts/import_library.py $ARGS --verify     # resolve every committed ref
```

Flags: `--base-url` (default `http://127.0.0.1:8192`), `--state`, `--only GLOB`; token from `--token-file` or `ASSETSTUDIO_IMPORT_TOKEN`. Exit code is non-zero on any failed or conflicting asset.

## GDScript client smoke (AS-06)

Real listener (`STUDIO_ENGINE=none`, integration on loopback), `ASResolver.prepare()` on three exact refs. Downloaded bytes are identical to the source files:

| Source | Bytes | sha256 |
|---|---:|---|
| `assets/nature/trees/pine_open_A.glb` | 406912 | `d94cc465efb29a3b5e68d938064822d5c8fbe198122ece7e97ac5048aec0759e` |
| `spruce_trees/models/dead_L_std_gapped_s3.glb` | 149904 | `21382404b26a8354c01f93130a1adbc3daa2be001c2f3bbc07a4d57c28270ce1` |
| `props/campfire.glb` | 15693652 | `7104e46c0601dac3f25bace636f3c5d92877c99010be014af2d1a18fbc982d84` |

No token appears in the Godot or server logs.
