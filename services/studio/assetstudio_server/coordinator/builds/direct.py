"""Deterministic direct-transform builds (variants): a confirmed GLB scale or raster resize/pad of the bound source
bytes. CPU only, no model, engine or worker; the source is never modified."""
from __future__ import annotations

from typing import Any

import numpy as np
from assetstudio_processing.glb import validate_glb_bytes
from assetstudio_processing.images import inspect_image, thumbnail_png
from assetstudio_processing.raster import RasterError, apply_raster_transform, decode_rgba
from assetstudio_processing.render import preview_png
from assetstudio_processing.transforms import TransformRejected, apply_glb_transform, preservation_checks

from .common import BuildInput

DUPLICATE_DETAIL = "identical to the source; confirm a duplicate identity explicitly"
EPS = 1e-9


def _record_source(inp: BuildInput) -> None:
    inp.meta.update(style_application="source_preserved", source=inp.bound.get("source", {}))


def check_not_duplicate(inp: BuildInput, identical: bool) -> None:
    """A no-op result is only a new identity when the row explicitly says so."""
    if identical and not inp.bound.get("confirm_duplicate"):
        inp.check("not_duplicate", False, DUPLICATE_DETAIL)
    else:
        inp.check("not_duplicate", True, "duplicate confirmed" if identical else "differs from the source")


def direct_glb(inp: BuildInput) -> None:
    transform = inp.bound["transform"]
    try:
        out, report = apply_glb_transform(inp.data, transform)
    except TransformRejected as e:
        inp.check("transform", False, f"{e.code}: {e}"[:300])
        return
    art = inp.env.ctx.store.register_artifact(
        out, "model", "model/gltf-binary", lineage=[inp.source.id], meta={"transform": transform},
        artifact_id=inp.artifact_id("model"))
    inp.roles["model"] = art.id
    for c in preservation_checks(inp.data, out):
        inp.check(c["id"], c["ok"], c.get("detail", ""))
    if "height_ok" in report:
        inp.check("height_target", report["height_ok"], f"{report['output_height']:.6g} m vs "
                  f"{transform['height_m']:g} m (tolerance {report['tolerance']:.2g})")
    v = validate_glb_bytes(out, require_texture=False)  # never demand a texture the source did not have
    for c in v["checks"]:
        inp.check(c["id"], c["ok"], c.get("detail", ""), advisory=not c["required"])
    check_not_duplicate(inp, all(abs(s - 1.0) < EPS for s in report["effective_scale"]))
    inp.preview = lambda: preview_png(out)
    _record_source(inp)
    inp.meta["transform"] = report


def _has_alpha(rgba: np.ndarray) -> bool:
    return bool((rgba[..., 3] < 255).any())


def _size_ok(transform: dict[str, Any], size: tuple[int, int]) -> tuple[bool, str]:
    w, h = size
    if transform["op"] == "pad_canvas":
        tw, th = transform["width"], transform["height"]
        return (w, h) == (tw, th), f"{w}x{h} (canvas {tw}x{th})"
    mw, mh = transform["max_width"], transform["max_height"]
    return w <= mw and h <= mh, f"{w}x{h} (box {mw}x{mh})"


def direct_raster(inp: BuildInput) -> None:
    transform = inp.bound["transform"]
    try:
        out, report = apply_raster_transform(inp.data, transform)
    except RasterError as e:
        inp.check("transform", False, str(e)[:300])
        return
    info = inspect_image(out, ("PNG",))
    inp.check("decode", (info.width, info.height) == tuple(report["output_size"]),
              f"{info.format} {info.width}x{info.height}")
    ok, detail = _size_ok(transform, (info.width, info.height))
    inp.check("output_size", ok, detail)
    src_px, out_px = decode_rgba(inp.data, "RGBA"), decode_rgba(out, "RGBA")
    if _has_alpha(src_px):
        inp.check("alpha_preserved", _has_alpha(out_px), "source transparency kept")
    check_not_duplicate(inp, src_px.shape == out_px.shape and bool(np.array_equal(src_px, out_px)))
    art = inp.env.ctx.store.register_artifact(
        out, "image", "image/png", lineage=[inp.source.id], meta={"width": info.width, "height": info.height,
                                                                   "transform": transform},
        artifact_id=inp.artifact_id("image"))
    inp.roles["image"] = art.id
    inp.preview = lambda: thumbnail_png(out)
    _record_source(inp)
    inp.meta["transform"] = report
