"""Per-kind build derivations. Each reads only the bound approved bytes and records non-overridable checks."""
from __future__ import annotations

import numpy as np
from assetstudio_processing.images import inspect_image, load_rgb_array, thumbnail_png
from assetstudio_processing.raster import (
    apply_mask,
    decode_rgba,
    fit_canvas,
    seam_stats,
    square_variants,
    tile_preview,
    trim,
)

from .common import BuildInput, foreground_mask
from .model3d import model3d


def _preview(inp: BuildInput, png: bytes) -> None:
    art = inp.env.ctx.store.register_artifact(thumbnail_png(png), "preview", "image/png", lineage=[inp.source.id],
                                              meta={"derived": "thumbnail 384px"})
    inp.roles["preview"] = art.id


def _decode(inp: BuildInput) -> bool:
    info = inspect_image(inp.data, ("PNG", "JPEG"))
    return inp.check("decode", True, f"{info.format} {info.width}x{info.height}")


def passthrough(inp: BuildInput) -> None:
    """concept.default: the approved original is the final image."""
    if _decode(inp):
        inp.roles["image"] = inp.source.id
        _preview(inp, inp.data)


def _alpha_checks(inp: BuildInput, rgba: np.ndarray, threshold: int, label: str) -> None:
    a = rgba[..., 3]
    inp.check(f"{label}_has_transparency", bool((a == 0).any()), "some pixels fully transparent")
    cover = float((a >= threshold).mean())
    inp.check(f"{label}_coverage", 0.0 < cover < 1.0, f"{cover:.1%} of pixels opaque")


def sprite(inp: BuildInput) -> None:
    _decode(inp)
    p = inp.params
    cut = apply_mask(load_rgb_array(inp.data), foreground_mask(inp))
    content, box = trim(cut, int(p["alpha_threshold"]))
    canvas, pivot = fit_canvas(content, int(p["canvas"]), float(p["padding"]), p["pivot"])
    if int(p["canvas"]) > 0:
        inp.check("canvas_size", canvas.shape[:2] == (p["canvas"], p["canvas"]), f"{canvas.shape[1]}x{canvas.shape[0]}")
    _alpha_checks(inp, canvas, int(p["alpha_threshold"]), "sprite")
    inp.add_png("image", canvas, {"pivot": pivot})
    _preview(inp, inp.env.ctx.store.artifact_bytes(inp.roles["image"]))
    inp.meta.update(pivot={"mode": p["pivot"], "xy": pivot}, source_bbox=box,
                    canvas=[int(canvas.shape[1]), int(canvas.shape[0])])


def icon(inp: BuildInput) -> None:
    _decode(inp)
    p = inp.params
    sizes = [int(s) for s in p["sizes"]]
    if p["background"] == "transparent":
        content, _ = trim(apply_mask(load_rgb_array(inp.data), foreground_mask(inp)), 128)
        base, _ = fit_canvas(content, max(sizes), float(p["padding"]), "center")
    else:
        rgb = decode_rgba(inp.data, "RGBA")  # PIL decodes JPEG too; alpha becomes opaque
        side = min(rgb.shape[:2])
        y, x = (rgb.shape[0] - side) // 2, (rgb.shape[1] - side) // 2
        base = rgb[y:y + side, x:x + side]
    variants = square_variants(base, sizes)
    for s, arr in variants.items():
        inp.check(f"size_{s}", arr.shape[:2] == (s, s), f"{arr.shape[1]}x{arr.shape[0]}")
        inp.add_png(f"icon_{s}", arr, {"size": s})
    inp.roles["image"] = inp.roles[f"icon_{max(sizes)}"]
    if p["background"] == "transparent":
        _alpha_checks(inp, variants[max(sizes)], 128, "icon")
    _preview(inp, inp.env.ctx.store.artifact_bytes(inp.roles["image"]))
    inp.meta.update(sizes=sorted(variants, reverse=True), background=p["background"])


def material(inp: BuildInput) -> None:
    info = inspect_image(inp.data, ("PNG", "JPEG"))
    inp.check("decode", True, f"{info.format} {info.width}x{info.height}")
    p = inp.params
    if p.get("require_square", True):
        inp.check("square", info.width == info.height, f"{info.width}x{info.height}")
    rgb = load_rgb_array(inp.data)
    seam = seam_stats(rgb)
    limit = float(p["seam_max_ratio"])
    inp.check("seamless", seam["ratio"] <= limit, f"seam ratio {seam['ratio']:.2f} (limit {limit:g})")
    inp.roles["base_color"] = inp.source.id
    inp.add_png("preview", tile_preview(rgb, 3, 768), {"derived": "3x3 tiling preview"})
    inp.meta.update(maps={"base_color": "generated"}, seam=seam,
                    derived_maps="not generated: no verified local PBR derivation")


BUILDS = {"passthrough": passthrough, "sprite": sprite, "icon": icon, "material": material, "model3d": model3d}
