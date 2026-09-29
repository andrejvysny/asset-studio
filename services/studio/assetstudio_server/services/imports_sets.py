"""Multi-file imports: frame sequences (→ sprite sheet / VFX atlas) and material bundles (explicit map roles)."""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from assetstudio_core.canonical import pretty_json
from assetstudio_core.kinds import Kind
from assetstudio_core.recipes import DEFAULT_RECIPE_FOR_KIND, RECIPES, validate_parameters
from assetstudio_processing import atlas
from assetstudio_processing.images import ImageRejected, inspect_image, thumbnail_png
from assetstudio_processing.raster import to_png

from ..errors import ApiError
from ..registry import ProjectContext

FRAME_KINDS = (Kind.sprite_sheet, Kind.vfx_flipbook)
MAP_ROLES = ("base_color", "normal", "roughness", "metallic", "ao", "height")
_SUFFIX = [(r"(base_?colou?r|albedo|diffuse|color|diff)$", "base_color"), (r"(normal|nrm|nor)(_?gl|_?dx)?$", "normal"),
           (r"(roughness|rough|rgh)$", "roughness"), (r"(metallic|metalness|metal)$", "metallic"),
           (r"(ao|occlusion|ambient_?occlusion)$", "ao"), (r"(height|disp|displacement|bump)$", "height")]


def suggest_role(filename: str) -> str | None:
    stem = filename.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    return next((role for pat, role in _SUFFIX if re.search(r"(^|[_\-. ])" + pat, stem)), None)


def expand(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """A single .zip upload is unpacked (with archive limits); plain files pass through."""
    if len(files) == 1 and files[0][0].lower().endswith(".zip"):
        return atlas.read_zip(files[0][1])
    return files


def preview_frames(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    try:
        names, frames = atlas.decode_frames(expand(files))
    except atlas.FrameError as e:
        return {"format": "frames", "ok": False, "allowed_kinds": [k.value for k in FRAME_KINDS],
                "validation": {"ok": False, "checks": [{"id": "frames", "ok": False, "detail": str(e)}]}}
    h, w = frames[0].shape[:2]
    return {"format": "frames", "ok": True, "allowed_kinds": [k.value for k in FRAME_KINDS],
            "suggested_kind": Kind.sprite_sheet.value, "frames": {"count": len(names), "size": [w, h], "order": names},
            "validation": {"ok": True, "checks": [{"id": "frames", "ok": True,
                                                   "detail": f"{len(names)} frames, {w}x{h}"}]}}


ArtifactIds = Callable[[str], str]  # role -> derived artifact id (replay-safe registration)


def reject_duplicate_names(files: list[tuple[str, bytes]]) -> None:
    """After normalization two uploads may share a name; a dict keyed by name would silently drop one."""
    seen: dict[str, int] = {}
    for n, _ in files:
        seen[n.lower()] = seen.get(n.lower(), 0) + 1
    if dup := sorted(n for n, c in seen.items() if c > 1):
        raise ApiError(422, "duplicate_names", f"several files share the (normalized) name {dup[:5]}; rename them",
                       dup)


def preview_material(files: list[tuple[str, bytes]]) -> dict[str, Any]:
    maps, checks = [], []
    try:
        atlas.preflight_decoded(files)
    except atlas.FrameError as e:
        return {"format": "material_bundle", "ok": False, "allowed_kinds": [Kind.material.value],
                "validation": {"ok": False, "checks": [{"id": "budget", "ok": False, "detail": str(e)}]}}
    for name, data in files:
        try:
            info = inspect_image(data)
            maps.append({"filename": name, "suggested_role": suggest_role(name), "image": info.as_meta()})
            checks.append({"id": f"decode:{name}", "ok": True})
        except ImageRejected as e:
            checks.append({"id": f"decode:{name}", "ok": False, "detail": str(e)})
    dims = {(m["image"]["width"], m["image"]["height"]) for m in maps}
    checks.append({"id": "uniform_size", "ok": len(dims) <= 1, "detail": ", ".join(f"{w}x{h}" for w, h in dims)})
    ok = all(c["ok"] for c in checks)
    return {"format": "material_bundle", "ok": ok, "allowed_kinds": [Kind.material.value],
            "suggested_kind": Kind.material.value, "maps": maps, "map_roles": list(MAP_ROLES),
            "validation": {"ok": ok, "checks": checks}}


def stage(d: Path, files: list[tuple[str, bytes]]) -> list[str]:
    (d / "files").mkdir(parents=True)
    for i, (_, data) in enumerate(files):
        (d / "files" / f"{i:04d}").write_bytes(data)
    return [n for n, _ in files]


def _staged(d: Path, names: list[str]) -> list[tuple[str, bytes]]:
    return [(n, (d / "files" / f"{i:04d}").read_bytes()) for i, n in enumerate(names)]


def frame_roles(ctx: ProjectContext, d: Path, meta: dict[str, Any], kind: Kind, overrides: dict[str, Any],
                provenance: dict[str, Any], aid: ArtifactIds) -> tuple[dict[str, str], dict[str, Any]]:
    recipe = RECIPES[DEFAULT_RECIPE_FOR_KIND[kind]]
    cfg, _ = ctx.config()
    pipe = cfg.pipelines.get(recipe.id)
    params = {**recipe.default_params(), **(pipe.parameters if pipe else {}), **overrides}
    if errors := validate_parameters(recipe, overrides):
        raise ApiError(422, "invalid_parameters", "; ".join(f"{k}: {m}" for k, m in errors))
    expanded = expand(_staged(d, meta["staged"]))
    names, frames = atlas.decode_frames(expanded)
    by_name = dict(expanded)
    try:
        img, rects, cols, rows = atlas.pack_grid(frames, int(params["columns"]), int(params["padding"]),
                                                 bool(params["pow2"]))
    except atlas.FrameError as e:
        raise ApiError(422, "atlas_invalid", str(e)) from e
    fields = {k: params[k] for k in ("fps", "pivot", "blend") if k in params}
    doc = atlas.atlas_meta(names, rects, (img.shape[1], img.shape[0]), grid=[cols, rows], **fields)
    png = to_png(img)
    store = ctx.store
    src = [store.register_artifact(data, "frame", "image/png", meta={"index": i, "source": n}, source=provenance,
                                   artifact_id=aid(f"frame_{i:04d}")).id
           for i, (n, data) in enumerate((n, by_name[n]) for n in names)]
    roles = {"atlas": store.register_artifact(png, "atlas", "image/png", lineage=src, artifact_id=aid("atlas"),
                                              meta={"width": img.shape[1], "height": img.shape[0]}).id}
    roles["meta"] = store.register_artifact(pretty_json(doc), "meta", "application/json", lineage=src,
                                            artifact_id=aid("meta")).id
    roles["preview"] = store.register_artifact(thumbnail_png(png), "preview", "image/png", lineage=[roles["atlas"]],
                                               artifact_id=aid("preview")).id
    for i, art_id in enumerate(src):
        roles[f"frame_{i:04d}"] = art_id
    checks = [{"id": "frames", "ok": True, "detail": f"{len(names)} frames {frames[0].shape[1]}x{frames[0].shape[0]}"},
              {"id": "atlas_size", "ok": True, "detail": f"{img.shape[1]}x{img.shape[0]} ≤ {atlas.MAX_ATLAS_PX}"}]
    return roles, {"ok": True, "checks": checks, "parameters": params}


def material_roles(ctx: ProjectContext, d: Path, meta: dict[str, Any], mapping: dict[str, str],
                   provenance: dict[str, Any], aid: ArtifactIds) -> tuple[dict[str, str], dict[str, Any]]:
    """Every staged file must be mapped explicitly (by upload id or its unique name); base_color is required;
    roles are unique."""
    names = meta["staged"]
    by_upload = dict(zip(meta.get("upload_ids", []), names, strict=False))
    mapping = {by_upload.get(k, k): v for k, v in mapping.items()}
    if len(set(names)) != len(names):
        raise ApiError(422, "duplicate_names", "the staged files have colliding names; upload again")
    if set(mapping) != set(names):
        raise ApiError(422, "unmapped_files", f"map every file to a role: {sorted(set(names) - set(mapping))}")
    roles_used = list(mapping.values())
    if bad := [r for r in roles_used if r not in MAP_ROLES]:
        raise ApiError(422, "unknown_role", f"unknown map roles {bad}; allowed {list(MAP_ROLES)}")
    if len(set(roles_used)) != len(roles_used):
        raise ApiError(422, "duplicate_role", "each map role can be used once")
    if "base_color" not in roles_used:
        raise ApiError(422, "missing_base_color", "a material needs a base_color map")
    staged = _staged(d, names)
    sizes = {n: inspect_image(data) for n, data in staged}
    dims = {(i.width, i.height) for i in sizes.values()}
    if len(dims) > 1:
        raise ApiError(422, "size_mismatch", "all maps must share one size: " +
                       ", ".join(f"{n} {i.width}x{i.height}" for n, i in sizes.items()))
    roles: dict[str, str] = {}
    for name, data in staged:
        role = mapping[name]
        roles[role] = ctx.store.register_artifact(data, role, sizes[name].mime, meta={"map": role, "source": name,
                                                                                    **sizes[name].as_meta()},
                                                  source=provenance, artifact_id=aid(role)).id
    base = ctx.store.artifact_bytes(roles["base_color"])
    roles["preview"] = ctx.store.register_artifact(thumbnail_png(base), "preview", "image/png",
                                                   lineage=[roles["base_color"]], artifact_id=aid("preview")).id
    (w, h), = dims
    return roles, {"ok": True, "checks": [{"id": "decode", "ok": True}, {"id": "uniform_size", "ok": True,
                                                                          "detail": f"{w}x{h}"}]}
