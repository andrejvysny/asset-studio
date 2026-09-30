"""Material policy on GLB bytes: verified from the output JSON and texture pixels, never from the code path."""
from __future__ import annotations

import io

import numpy as np
import pytest
import trimesh
from assetstudio_processing.materials import (
    MaterialRejected,
    _decode,
    _texture_image,
    apply_material_policy,
    preservation_checks,
    read_glb,
)
from PIL import Image


def _glb(alpha: np.ndarray | None = None, rough: int = 100, metal: int = 200, textured: bool = True) -> bytes:
    size = 16
    a = alpha if alpha is not None else np.full((size, size), 255, np.uint8)
    base = np.dstack([np.full((size, size, 3), 120, np.uint8), a])
    mr = np.dstack([np.zeros((size, size), np.uint8), np.full((size, size), rough, np.uint8),
                    np.full((size, size), metal, np.uint8)])
    kw = dict(baseColorTexture=Image.fromarray(base, "RGBA"), metallicRoughnessTexture=Image.fromarray(mr, "RGB")) \
        if textured else dict(baseColorFactor=[200, 200, 200, 255])
    mat = trimesh.visual.material.PBRMaterial(metallicFactor=1.0, roughnessFactor=1.0, alphaMode="OPAQUE", **kw)
    v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0]], float)
    f = np.array([[0, 1, 2], [0, 2, 3]])
    uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float)
    tm = trimesh.Trimesh(vertices=v, faces=f, process=False, visual=trimesh.visual.TextureVisuals(uv=uv, material=mat))
    buf = io.BytesIO()
    tm.export(buf, file_type="glb")
    return buf.getvalue()


def _mat(glb: bytes) -> dict:
    return read_glb(glb)[0]["materials"][0]


def _mr_pixels(glb: bytes) -> np.ndarray:
    doc, payload = read_glb(glb)
    return _decode(doc, payload, _texture_image(doc, doc["materials"][0]["pbrMetallicRoughness"]
                                                ["metallicRoughnessTexture"]))


def test_mask_cutoff_and_culling_json_only() -> None:
    src = _glb()
    out, rep = apply_material_policy(src, {"alpha_mode": "mask", "alpha_cutoff": 0.4, "double_sided": True})
    m = _mat(out)
    assert m["alphaMode"] == "MASK" and m["alphaCutoff"] == 0.4 and m["doubleSided"] is True
    assert rep["rewritten_views"] == []
    assert all(c["ok"] for c in preservation_checks(src, out, rep["rewritten_views"]))


def test_auto_alpha_measures_texels() -> None:
    cut = np.full((16, 16), 255, np.uint8)
    cut[:4] = 0  # 25% transparent: a leaf card
    out, rep = apply_material_policy(_glb(cut), {"alpha_mode": "auto"})
    assert _mat(out)["alphaMode"] == "MASK" and rep["materials"][0]["auto"]["transparent_fraction"] == 0.25
    out, rep = apply_material_policy(_glb(), {"alpha_mode": "auto"})
    assert _mat(out)["alphaMode"] == "OPAQUE" and "alphaCutoff" not in _mat(out)


def test_roughness_clamp_rewrites_linear_texture() -> None:
    src = _glb(rough=100)  # 100/255 = 0.392 effective
    out, rep = apply_material_policy(src, {"roughness_min": 0.75})
    g = _mr_pixels(out)[..., 1]
    assert int(g.min()) == int(g.max()) == 192  # ceil(0.75 * 255): linear value, no sRGB curve, never below min
    assert _mat(out)["pbrMetallicRoughness"]["roughnessFactor"] == 1.0
    assert rep["materials"][0]["roughness_before"]["median"] == pytest.approx(100 / 255, abs=1e-3)
    checks = preservation_checks(src, out, rep["rewritten_views"])
    assert all(c["ok"] for c in checks), checks
    assert len(rep["rewritten_views"]) == 1
    trimesh.load(io.BytesIO(out), file_type="glb", force="scene")  # still a loadable GLB


def test_metallic_replace_is_exact() -> None:
    out, _ = apply_material_policy(_glb(metal=200), {"metallic": 0.0})
    assert _mat(out)["pbrMetallicRoughness"]["metallicFactor"] == 0.0
    assert int(_mr_pixels(out)[..., 2].min()) == 255  # factor x 1 = the replaced value


def test_factor_only_material() -> None:
    out, rep = apply_material_policy(_glb(textured=False), {"roughness_max": 0.5, "metallic": 0.1})
    pbr = _mat(out)["pbrMetallicRoughness"]
    assert pbr["roughnessFactor"] == 0.5 and pbr["metallicFactor"] == 0.1 and rep["rewritten_views"] == []


def test_empty_policy_is_byte_stable() -> None:
    src = _glb()
    out, rep = apply_material_policy(src, {})
    assert read_glb(out)[1][:len(read_glb(src)[1])] == read_glb(src)[1]
    assert rep["rewritten_views"] == [] and all(c["ok"] for c in preservation_checks(src, out, []))


def test_no_materials_rejected() -> None:
    tm = trimesh.Trimesh(vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]], process=False)
    buf = io.BytesIO()
    tm.export(buf, file_type="glb")
    doc, payload = read_glb(buf.getvalue())
    doc.pop("materials", None)
    from assetstudio_processing.materials import write_glb

    with pytest.raises(MaterialRejected):
        apply_material_policy(write_glb(doc, payload), {"alpha_mode": "mask"})


def test_roughness_bounds_round_inward() -> None:
    out, _ = apply_material_policy(_glb(rough=100), {"roughness_min": 0.7})
    assert int(_mr_pixels(out)[..., 1].min()) / 255 >= 0.7  # 0.7 * 255 = 178.5 -> 179
    out, _ = apply_material_policy(_glb(rough=250), {"roughness_max": 0.3})
    assert int(_mr_pixels(out)[..., 1].max()) / 255 <= 0.3
