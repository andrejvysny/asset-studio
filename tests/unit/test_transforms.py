"""Deterministic GLB scale, raster resize/pad and single-view render. Fixtures are assembled in-test."""
from __future__ import annotations

import copy
import io
import json
import math
import struct
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
import trimesh
from assetstudio_core.variants import AxisScale, PadCanvas, ResizeKeepAspect, TargetHeight, UniformScale
from assetstudio_processing import raster, render
from assetstudio_processing.transforms import (
    TransformRejected,
    apply_glb_transform,
    inspect_static_glb,
    preservation_checks,
)
from PIL import Image

CUBE_IDX = np.array([0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4, 3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3,
                     1, 2, 6, 1, 6, 5], dtype="<u2")
CORNERS = np.array([[x, y, z] for z in (0, 1) for y in (0, 1) for x in (0, 1)], dtype="<f4")
CORNERS = CORNERS[[0, 1, 3, 2, 4, 5, 7, 6]]  # ring order used by CUBE_IDX


def _png() -> bytes:
    out = io.BytesIO()
    Image.fromarray(np.full((4, 4, 3), 180, np.uint8)).save(out, "PNG")
    return out.getvalue()


def pack_glb(doc: dict[str, Any], blob: bytes) -> bytes:
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    blob += b"\0" * (-len(blob) % 4)
    total = 12 + 8 + len(body) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total) + struct.pack("<I4s", len(body), b"JSON") + body
            + struct.pack("<I4s", len(blob), b"BIN\0") + blob)


def build_glb(alpha_mode: str = "BLEND", size: float = 1.0, mutate: Callable[[dict], None] | None = None) -> bytes:
    """Mesh A: centred cube (tight positions at a byte offset). Mesh B: unit cube, interleaved pos+uv (stride 20)."""
    a = ((CORNERS - 0.5) * size).astype("<f4")
    b = (CORNERS * size).astype("<f4")
    uv = CORNERS[:, :2].astype("<f4")
    idx = CUBE_IDX.tobytes()
    idx += b"\0" * (-len(idx) % 4)
    parts = [idx, b"\xaa\xbb\xcc\xdd", a.tobytes(), uv.tobytes()]
    inter = b"".join(b[i].tobytes() + uv[i].tobytes() for i in range(8))
    png = _png()
    offs, pos = [], 0
    for p in (*parts, inter, png + b"\0" * (-len(png) % 4)):
        offs.append(pos)
        pos += len(p)
    blob = b"".join((*parts, inter, png + b"\0" * (-len(png) % 4)))
    h = math.sqrt(0.5)
    doc: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "test"}, "extras": {"keep": [1, 2]},
        "extensionsUsed": ["KHR_materials_emissive_strength"],
        "scene": 0, "scenes": [{"nodes": [0, 2], "name": "s"}],
        "nodes": [{"name": "parent", "translation": [1, 2, 3], "rotation": [0, h, 0, h], "scale": [2, 2, 2],
                   "mesh": 0, "children": [1]},
                  {"name": "child", "matrix": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 1, 0, 1], "mesh": 1},
                  {"name": "other", "translation": [-5, 0, 0], "mesh": 1}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 1, "TEXCOORD_0": 2}, "indices": 0, "material": 0}]},
                   {"primitives": [{"attributes": {"POSITION": 3, "TEXCOORD_0": 4}, "indices": 0, "material": 1}]}],
        "materials": [{"name": "m0", "alphaMode": alpha_mode, "doubleSided": True,
                       "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "baseColorFactor": [1, 1, 1, .5]}},
                      {"name": "m1", "alphaMode": "MASK", "alphaCutoff": 0.3,
                       "pbrMetallicRoughness": {"baseColorFactor": [.2, .6, .9, 1]}}],
        "textures": [{"source": 0, "sampler": 0}], "images": [{"bufferView": 5, "mimeType": "image/png"}],
        "samplers": [{"magFilter": 9728, "minFilter": 9728, "wrapS": 33071, "wrapT": 10497}],
        "accessors": [{"bufferView": 0, "componentType": 5123, "count": 36, "type": "SCALAR"},
                      {"bufferView": 2, "byteOffset": 0, "componentType": 5126, "count": 8, "type": "VEC3"},
                      {"bufferView": 3, "componentType": 5126, "count": 8, "type": "VEC2"},
                      {"bufferView": 4, "componentType": 5126, "count": 8, "type": "VEC3"},
                      {"bufferView": 4, "byteOffset": 12, "componentType": 5126, "count": 8, "type": "VEC2"}],
        "bufferViews": [{"buffer": 0, "byteOffset": offs[0], "byteLength": len(idx)},
                        {"buffer": 0, "byteOffset": offs[1], "byteLength": 4},
                        {"buffer": 0, "byteOffset": offs[2], "byteLength": 96},
                        {"buffer": 0, "byteOffset": offs[3], "byteLength": 64},
                        {"buffer": 0, "byteOffset": offs[4], "byteLength": len(inter), "byteStride": 20},
                        {"buffer": 0, "byteOffset": offs[5], "byteLength": len(png)}],
        "buffers": [{"byteLength": len(blob)}],
    }
    if mutate:
        mutate(doc)
    return pack_glb(doc, blob)


def bounds(info: dict[str, Any]) -> tuple[np.ndarray, np.ndarray]:
    return np.array(info["bounds"]["min"]), np.array(info["bounds"]["max"])


def test_inspect_exact_world_bounds() -> None:
    info = inspect_static_glb(build_glb())
    lo, hi = bounds(info)
    assert np.allclose(lo, [-5, 0, 0], atol=1e-5) and np.allclose(hi, [3, 6, 4], atol=1e-5)
    assert info["nodes"] == 3 and info["vertex_count"] == 8 * 3 and info["meshes"] == 2
    assert info["alpha_modes"] == ["BLEND", "MASK"] and info["roots"] == [0, 2]


def test_vt01_uniform_scale_and_payload_identical() -> None:
    src = build_glb()
    out, rep = apply_glb_transform(src, UniformScale(factor=2.0))
    lo, hi = bounds({"bounds": rep["output_bounds"]})
    assert np.allclose(lo, [-10, 0, 0], atol=1e-4) and np.allclose(hi, [6, 12, 8], atol=1e-4)
    assert rep["effective_scale"] == [2.0, 2.0, 2.0] and rep["anchor_point"] == [0.0, 0.0, 0.0]
    assert inspect_static_glb(out)["nodes"] == 4
    checks = preservation_checks(src, out)
    assert all(c["ok"] for c in checks), checks
    assert len(out) % 4 == 0 and struct.unpack_from("<I", out, 8)[0] == len(out)
    assert json.loads(out[20:20 + struct.unpack_from("<I", out, 12)[0]])["nodes"][-1]["name"] == "assetstudio_variant_transform"


def test_vt02_axis_scale_bottom_center_anchor() -> None:
    _, rep = apply_glb_transform(build_glb(), AxisScale(x=2, y=3, z=0.5, anchor="bottom_center"))
    (ilo, ihi), (olo, ohi) = bounds({"bounds": rep["input_bounds"]}), bounds({"bounds": rep["output_bounds"]})
    assert olo[1] == pytest.approx(ilo[1], abs=1e-5)
    assert (olo[0] + ohi[0]) / 2 == pytest.approx((ilo[0] + ihi[0]) / 2, abs=1e-5)
    assert (olo[2] + ohi[2]) / 2 == pytest.approx((ilo[2] + ihi[2]) / 2, abs=1e-5)
    assert np.allclose(ohi - olo, (ihi - ilo) * [2, 3, 0.5], atol=1e-4)
    assert np.allclose(rep["anchor_point"], [-1, 0, 2], atol=1e-5)


def test_bounds_center_anchor_keeps_center() -> None:
    _, rep = apply_glb_transform(build_glb(), UniformScale(factor=0.5, anchor="bounds_center"))
    (ilo, ihi), (olo, ohi) = bounds({"bounds": rep["input_bounds"]}), bounds({"bounds": rep["output_bounds"]})
    assert np.allclose((ilo + ihi) / 2, (olo + ohi) / 2, atol=1e-5)


def test_vt03_target_height_with_existing_transforms() -> None:
    src = build_glb()
    out, rep = apply_glb_transform(src, TargetHeight(height_m=1.8, units_confirmed=True, anchor="bottom_center"))
    assert rep["height_ok"] and rep["input_height"] == pytest.approx(6.0, abs=1e-5)
    assert bounds(inspect_static_glb(out))[1][1] - bounds(inspect_static_glb(out))[0][1] == pytest.approx(1.8, abs=1e-5)
    assert rep["effective_scale"][0] == pytest.approx(0.3, rel=1e-5)


def test_vt03_target_height_requires_units_confirmed() -> None:
    with pytest.raises(TransformRejected) as e:
        apply_glb_transform(build_glb(), TargetHeight(height_m=2.0))
    assert e.value.code == "invalid_transform"


def test_invalid_transform_dicts_rejected() -> None:
    for bad in ({"op": "uniform_scale", "factor": float("nan")}, {"op": "axis_scale", "x": -1, "y": 1, "z": 1},
                {"op": "nope"}):
        with pytest.raises(TransformRejected) as e:
            apply_glb_transform(build_glb(), bad)
        assert e.value.code == "invalid_transform"
    out, _ = apply_glb_transform(build_glb(), {"op": "uniform_scale", "factor": 3})
    assert inspect_static_glb(out)["nodes"] == 4


def test_vt04_degenerate_and_nan_are_corrupt() -> None:
    with pytest.raises(TransformRejected) as e:
        inspect_static_glb(build_glb(size=0.0, mutate=lambda d: d["scenes"][0].update(nodes=[2])))
    assert e.value.code == "corrupt_source"

    def nan_matrix(d: dict) -> None:
        d["nodes"][1]["matrix"][12] = float("nan")

    with pytest.raises(TransformRejected) as e:
        inspect_static_glb(build_glb(mutate=nan_matrix))
    assert e.value.code == "corrupt_source"
    with pytest.raises(TransformRejected) as e:
        inspect_static_glb(b"not a glb at all, sorry" * 3)
    assert e.value.code == "corrupt_source"


def test_vt06_materials_samplers_unchanged() -> None:
    src = build_glb()
    out, _ = apply_glb_transform(src, AxisScale(x=1, y=2, z=1))
    a = json.loads(src[20:20 + struct.unpack_from("<I", src, 12)[0]])
    b = json.loads(out[20:20 + struct.unpack_from("<I", out, 12)[0]])
    for key in ("materials", "samplers", "textures", "images", "accessors", "bufferViews", "meshes", "extras", "asset"):
        assert a[key] == b[key], key
    ids = {c["id"]: c["ok"] for c in preservation_checks(src, out)}
    assert ids["alpha_and_sampler_unchanged"] and ids["bin_identical"] and ids["extensions_unchanged"]
    assert b["materials"][0]["alphaMode"] == "BLEND" and b["materials"][0]["doubleSided"] is True


def test_preservation_detects_tampering() -> None:
    src = build_glb()
    out, _ = apply_glb_transform(src, UniformScale(factor=2))
    other = build_glb(alpha_mode="OPAQUE")
    other_out, _ = apply_glb_transform(other, UniformScale(factor=2))
    ids = {c["id"]: c["ok"] for c in preservation_checks(src, other_out)}
    assert not ids["alpha_and_sampler_unchanged"] and not ids["json_objects_unchanged"]
    assert all(c["ok"] for c in preservation_checks(src, out))


def _mut(fn: Callable[[dict], None]) -> bytes:
    return build_glb(mutate=fn)


REJECTS: dict[str, Callable[[dict], None]] = {
    "skin": lambda d: d.update(skins=[{"joints": [0]}]),
    "animation": lambda d: d.update(animations=[{"channels": [], "samplers": []}]),
    "morph": lambda d: d["meshes"][0]["primitives"][0].update(targets=[{"POSITION": 1}]),
    "external_uri": lambda d: d["buffers"][0].update(uri="scene.bin"),
    "required_ext": lambda d: d.update(extensionsRequired=["KHR_draco_mesh_compression"]),
    "quantized": lambda d: d["accessors"][1].update(componentType=5122),
    "sparse": lambda d: d["accessors"][1].update(sparse={"count": 1}),
    "lines": lambda d: d["meshes"][0]["primitives"][0].update(mode=1),
    "two_scenes": lambda d: (d.pop("scene"), d["scenes"].append({"nodes": []})),
}


@pytest.mark.parametrize("name", sorted(REJECTS))
def test_vt12_unsupported_features(name: str) -> None:
    with pytest.raises(TransformRejected) as e:
        apply_glb_transform(_mut(REJECTS[name]), UniformScale(factor=2))
    assert e.value.code == "unsupported_source_features", str(e.value)


def test_allowed_required_extension_and_out_of_range_data() -> None:
    inspect_static_glb(_mut(lambda d: d.update(extensionsRequired=["KHR_materials_unlit"])))

    def oob(d: dict) -> None:
        d["accessors"][1]["count"] = 10_000

    with pytest.raises(TransformRejected) as e:
        inspect_static_glb(_mut(oob))
    assert e.value.code == "corrupt_source"


def test_trimesh_loads_output_and_matches_bounds() -> None:
    out, rep = apply_glb_transform(build_glb(), TargetHeight(height_m=3.0, units_confirmed=True))
    scene = trimesh.load(io.BytesIO(out), file_type="glb", force="scene")
    assert np.allclose(scene.bounds, [rep["output_bounds"]["min"], rep["output_bounds"]["max"]], atol=1e-4)
    assert copy.deepcopy(rep["transform"])["op"] == "target_height"


# --- raster -----------------------------------------------------------------------------------------------------
def png_bytes(arr: np.ndarray) -> bytes:
    return raster.to_png(arr)


def test_resize_keep_aspect_alpha_no_halo() -> None:
    src = np.zeros((20, 40, 4), np.uint8)
    src[..., :3] = (250, 200, 50)
    src[:, :20, 3] = 255  # left half opaque, right half transparent with black RGB
    out = raster.resize_keep_aspect(src, 80, 80, "lanczos")
    assert out.shape[:2] == (40, 80)
    assert out[5, 75, 3] == 0
    edge = out[..., 3] > 200
    assert edge.any() and out[edge][:, :3].astype(int).min(0).tolist() >= [235, 185, 40]  # dark fringe would be far lower
    assert raster.resize_keep_aspect(src, 10, 100, "lanczos").shape[:2] == (5, 10)


def test_resize_nearest_exact_pixel_art() -> None:
    src = np.random.default_rng(1).integers(0, 255, (4, 6, 4), dtype=np.uint8)
    out = raster.resize_keep_aspect(src, 12, 8, "nearest")
    assert np.array_equal(out, src.repeat(2, 0).repeat(2, 1))


@pytest.mark.parametrize(("placement", "bounds_xywh", "pivot"), [
    ("center", [3, 4, 4, 2], [5, 5]), ("bottom_center", [3, 8, 4, 2], [5, 10]), ("top_left", [0, 0, 4, 2], [2, 1])])
def test_pad_placement(placement: str, bounds_xywh: list[int], pivot: list[int]) -> None:
    src = np.full((2, 4, 4), 255, np.uint8)
    out, info = raster.pad_canvas(src, 10, 10, placement, "transparent")
    assert info["content_bounds"] == bounds_xywh and info["pivot"] == pivot
    assert out[..., 3].sum() == 255 * 8 and out[bounds_xywh[1], bounds_xywh[0], 3] == 255


def test_pad_backgrounds_and_errors() -> None:
    src = np.zeros((2, 2, 4), np.uint8)
    src[..., :] = (10, 20, 30, 255)
    out, _ = raster.pad_canvas(src, 6, 6, "center", "#ff0080")
    assert tuple(out[0, 0]) == (255, 0, 128, 255) and tuple(out[2, 2]) == (10, 20, 30, 255)
    out, _ = raster.pad_canvas(src, 6, 6, "center", "source_edge")
    assert (out == (10, 20, 30, 255)).all()
    with pytest.raises(raster.RasterError, match="resize first"):
        raster.pad_canvas(src, 1, 6, "center", "transparent")


def test_apply_raster_transform_and_exif() -> None:
    src = np.zeros((3, 6, 4), np.uint8)
    src[..., 3] = 255
    data = png_bytes(src)
    out, meta = raster.apply_raster_transform(data, ResizeKeepAspect(max_width=12, max_height=12, resample="nearest"))
    assert meta["input_size"] == [6, 3] and meta["output_size"] == [12, 6] and meta["resample"] == "nearest"
    assert Image.open(io.BytesIO(out)).size == (12, 6)
    out, meta = raster.apply_raster_transform(data, PadCanvas(width=8, height=8, placement="bottom_center"))
    assert meta["content_bounds"] == [1, 5, 6, 3] and meta["output_size"] == [8, 8]
    # EXIF orientation 6 (rotate 90 CW) is applied to the derivative
    jpg = io.BytesIO()
    exif = Image.Exif()
    exif[0x0112] = 6
    Image.new("RGB", (8, 4), (9, 9, 9)).save(jpg, "JPEG", exif=exif)
    _, meta = raster.apply_raster_transform(jpg.getvalue(), {"op": "pad_canvas", "width": 10, "height": 10})
    assert meta["input_size"] == [4, 8]
    with pytest.raises(raster.RasterError):
        raster.apply_raster_transform(data, {"op": "pad_canvas", "width": 0, "height": 5})
    with pytest.raises(raster.RasterError):
        raster.apply_raster_transform(b"garbage", PadCanvas(width=5, height=5))


# --- render -----------------------------------------------------------------------------------------------------
def test_render_view_png_background_and_warnings() -> None:
    png, meta = render.render_view(build_glb(alpha_mode="BLEND"), -45, 20, size=128, background=(10, 200, 30))
    im = np.asarray(Image.open(io.BytesIO(png)))
    assert im.shape == (128, 128, 3)
    for y, x in ((0, 0), (0, 127), (127, 0), (127, 127)):
        assert tuple(im[y, x]) == (10, 200, 30)
    assert (im != (10, 200, 30)).any(axis=-1).sum() > 200
    assert {"alpha_mode_ignored", "double_sided_ignored"} <= set(meta["warnings"])
    assert meta["renderer"] == "assetstudio.cpu_lambert.v1" and meta["size"] == 128


def test_reference_views_and_no_warning_for_opaque() -> None:
    def clean(d: dict) -> None:
        for m in d["materials"]:
            m.pop("doubleSided", None)
            m["alphaMode"] = "OPAQUE"

    views = render.reference_views(build_glb(mutate=clean), size=64)
    assert set(views) == set(render.REFERENCE_VIEWS)
    assert all(Image.open(io.BytesIO(p)).size == (64, 64) for p, _ in views.values())
    assert not {"alpha_mode_ignored", "double_sided_ignored"} & set(views["side"][1]["warnings"])


def test_preview_png_unchanged_shape() -> None:
    assert Image.open(io.BytesIO(render.preview_png(build_glb(), size=64))).size == (128, 128)
