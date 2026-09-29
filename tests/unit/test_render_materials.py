"""Renderer v2: base colour factor x texture alpha x alpha modes, two-sided lighting, honest refusal.
Oracles are analytic (flat quads facing the camera; background pixels are exactly the background colour)."""
from __future__ import annotations

import io
import json
import struct
from collections.abc import Callable
from typing import Any

import numpy as np
import pytest
import trimesh
from assetstudio_processing import render
from assetstudio_processing.render import RenderUnsupported
from PIL import Image
from trimesh.visual.material import PBRMaterial

BG = (10, 200, 30)
SIZE = 64
LEFT, RIGHT = (32, 23), (32, 41)  # (row, col) of the left/right half centres of a card spanning ~13..51 px


def patch_json(glb: bytes, fn: Callable[[dict[str, Any]], None]) -> bytes:
    jlen = struct.unpack_from("<I", glb, 12)[0]
    doc = json.loads(glb[20:20 + jlen])
    fn(doc)
    body = json.dumps(doc).encode()
    body += b" " * (-len(body) % 4)
    rest = glb[20 + jlen:]
    return (struct.pack("<4sII", b"glTF", 2, 12 + 8 + len(body) + len(rest)) + struct.pack("<I4s", len(body), b"JSON")
            + body + rest)


def half_alpha_texture(size: int = 8) -> Image.Image:
    """White RGBA image whose left half is fully transparent."""
    a = np.full((size, size, 4), 255, np.uint8)
    a[:, : size // 2, 3] = 0
    return Image.fromarray(a)


def card(z: float, material: PBRMaterial, flip: bool = False) -> trimesh.Trimesh:
    v = np.array([[-1, -1, z], [1, -1, z], [1, 1, z], [-1, 1, z]], float)
    f = np.array([[0, 2, 1], [0, 3, 2]] if flip else [[0, 1, 2], [0, 2, 3]])
    uv = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float)
    return trimesh.Trimesh(v, f, visual=trimesh.visual.TextureVisuals(uv=uv, material=material), process=False)


def glb_of(*meshes: trimesh.Trimesh) -> bytes:
    return trimesh.Scene(list(meshes)).export(file_type="glb")


def px(data: bytes, yaw: float = 0, pitch: float = 0) -> tuple[np.ndarray, dict]:
    png, meta = render.render_view(data, yaw, pitch, size=SIZE, background=BG)
    return np.asarray(Image.open(io.BytesIO(png))), meta


def is_bg(im: np.ndarray, at: tuple[int, int]) -> bool:
    return tuple(im[at]) == BG


def test_mask_cutout_left_half_is_background_and_opaque_is_not() -> None:
    tex = half_alpha_texture()
    mask, _ = px(glb_of(card(0, PBRMaterial(baseColorTexture=tex, alphaMode="MASK", alphaCutoff=0.5))))
    assert is_bg(mask, LEFT) and not is_bg(mask, RIGHT)
    opaque, meta = px(glb_of(card(0, PBRMaterial(baseColorTexture=tex, alphaMode="OPAQUE"))))
    assert not is_bg(opaque, LEFT) and not is_bg(opaque, RIGHT)
    assert "blend_approximated" not in meta["warnings"]


def test_blend_is_approximated_as_mask_with_warning() -> None:
    im, meta = px(glb_of(card(0, PBRMaterial(baseColorTexture=half_alpha_texture(), alphaMode="BLEND"))))
    assert is_bg(im, LEFT) and not is_bg(im, RIGHT) and "blend_approximated" in meta["warnings"]


def test_factor_alpha_below_cutoff_discards_everything() -> None:
    im, _ = px(glb_of(card(0, PBRMaterial(baseColorFactor=[1.0, 1.0, 1.0, 0.2], alphaMode="MASK"))))
    assert is_bg(im, LEFT) and is_bg(im, RIGHT)


@pytest.mark.parametrize(("factor", "dominant"), [([1.0, 0.0, 0.0, 1.0], 0), ([0.0, 0.0, 1.0, 1.0], 2)])
def test_base_colour_factor_tints_white_texture(factor: list[float], dominant: int) -> None:
    white = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
    im, meta = px(glb_of(card(0, PBRMaterial(baseColorFactor=factor, baseColorTexture=white))))
    centre = im[32, 32].astype(int)
    others = [c for i, c in enumerate(centre) if i != dominant]
    assert centre[dominant] > 100 and others == [0, 0]
    assert "texture_missing" not in meta["warnings"]


def test_factor_only_and_vertex_colour_render_colour() -> None:
    im, meta = px(glb_of(card(0, PBRMaterial(baseColorFactor=[0.0, 1.0, 0.0, 1.0]))))
    assert im[32, 32, 1] > 100 and im[32, 32, 0] == 0 and "texture_missing" not in meta["warnings"]
    v = np.array([[-1, -1, 0], [1, -1, 0], [1, 1, 0], [-1, 1, 0]], float)
    m = trimesh.Trimesh(v, [[0, 1, 2], [0, 2, 3]], process=False)
    m.visual = trimesh.visual.ColorVisuals(m, vertex_colors=[[255, 0, 0, 255]] * 4)
    im, meta = px(glb_of(m))
    assert im[32, 32, 0] > 100 and im[32, 32, 2] == 0 and "texture_missing" not in meta["warnings"]


def test_flat_grey_render_warns_texture_missing() -> None:
    _, meta = px(glb_of(card(0, PBRMaterial())))
    assert "texture_missing" in meta["warnings"]


def test_masked_hole_in_front_card_shows_back_card() -> None:
    front = card(0.5, PBRMaterial(baseColorTexture=half_alpha_texture(), alphaMode="MASK"))
    back = card(-0.5, PBRMaterial(baseColorFactor=[1.0, 0.0, 0.0, 1.0]))
    im, _ = px(glb_of(front, back))
    left, right = im[LEFT].astype(int), im[RIGHT].astype(int)
    assert left[0] > 100 and left[1] == 0 and left[2] == 0  # red back card through the hole
    assert right[0] == right[1] == right[2] and right[0] > 100  # neutral front card


def test_back_faces_are_lit_not_culled() -> None:
    im, _ = px(glb_of(card(0, PBRMaterial(baseColorFactor=[1.0, 1.0, 1.0, 1.0]), flip=True)))
    assert not is_bg(im, (32, 32))
    away, _ = px(glb_of(card(0, PBRMaterial(baseColorFactor=[1.0, 1.0, 1.0, 1.0]))), yaw=180)
    assert not is_bg(away, (32, 32))


def test_required_unsupported_extension_is_refused() -> None:
    data = patch_json(glb_of(card(0, PBRMaterial())), lambda d: d.update(
        extensionsRequired=["KHR_draco_mesh_compression"], extensionsUsed=["KHR_draco_mesh_compression"]))
    with pytest.raises(RenderUnsupported) as e:
        render.render_view(data, 0, 0, size=32)
    assert e.value.code == "unsupported_source_features" and "KHR_draco_mesh_compression" in str(e.value)


def test_used_extensions_wrap_modes_and_missing_uv_warn() -> None:
    tex = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
    base = glb_of(card(0, PBRMaterial(baseColorTexture=tex)))

    def mutate(d: dict[str, Any]) -> None:
        d["extensionsUsed"] = ["KHR_materials_unlit", "KHR_texture_transform", "KHR_materials_ior"]
        d["samplers"] = [{"wrapS": 33071, "wrapT": 10497}]
        for me in d["meshes"]:
            for p in me["primitives"]:
                p["attributes"].pop("TEXCOORD_0", None)

    _, meta = px(patch_json(base, mutate))
    w = set(meta["warnings"])
    assert {"unsupported_extension:KHR_materials_unlit", "unsupported_extension:KHR_texture_transform",
            "sampler_wrap_ignored", "texture_unrendered"} <= w
    assert "unsupported_extension:KHR_materials_ior" not in w
    _, clean = px(base)
    assert clean["warnings"] == [] and clean["renderer"] == "assetstudio.cpu_lambert.v2"
    assert clean["culling"] == "none" and clean["lighting"] == "two-sided Lambert"
