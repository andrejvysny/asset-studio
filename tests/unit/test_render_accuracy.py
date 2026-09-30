"""Renderer colour accuracy: baseColorFactor in linear space, BLEND as real alpha compositing (nearest layer)."""
from __future__ import annotations

import io

import numpy as np
import trimesh
from assetstudio_processing import render
from assetstudio_processing.render_materials import linear_to_srgb, srgb_to_linear
from PIL import Image
from trimesh.visual.material import PBRMaterial

SIZE = 64
BG = (10, 200, 30)


def F(*v: float) -> list[float]:
    """Float factor: trimesh reads int lists as uint8 0..255."""
    return [float(x) for x in v]


def card(z: float, material: PBRMaterial) -> trimesh.Trimesh:
    v = np.array([[-1, -1, z], [1, -1, z], [1, 1, z], [-1, 1, z]], float)
    return trimesh.Trimesh(v, [[0, 1, 2], [0, 2, 3]], process=False,
                           visual=trimesh.visual.TextureVisuals(uv=np.array([[0, 0], [1, 0], [1, 1], [0, 1]], float),
                                                                material=material))


def centre(*meshes: trimesh.Trimesh) -> np.ndarray:
    glb = trimesh.Scene(list(meshes)).export(file_type="glb")
    png, _ = render.render_view(glb, 0, 0, size=SIZE, background=BG)
    return np.asarray(Image.open(io.BytesIO(png)))[32, 32].astype(np.float32)


def lin(px: np.ndarray) -> np.ndarray:
    return srgb_to_linear(px / 255)


def test_transfer_functions_round_trip() -> None:
    x = np.linspace(0, 1, 101, dtype=np.float32)
    assert np.allclose(linear_to_srgb(srgb_to_linear(x)), x, atol=1e-5)
    assert abs(float(linear_to_srgb(np.float32(0.5))) - 0.7354) < 1e-3


def test_factor_applies_in_linear_space() -> None:
    white = Image.new("RGBA", (4, 4), (255, 255, 255, 255))
    for tex in (white, None):
        full = centre(card(0, PBRMaterial(baseColorFactor=F(1, 1, 1, 1), baseColorTexture=tex)))
        half = centre(card(0, PBRMaterial(baseColorFactor=F(0.5, 0.5, 0.5, 1), baseColorTexture=tex)))
        ratio = lin(half) / lin(full)
        assert np.allclose(ratio, 0.5, atol=0.02), ratio
        assert half[0] > 0.6 * full[0]  # sRGB-space multiply would give ~0.5


def test_blend_mixes_in_linear_space() -> None:
    red = card(-0.5, PBRMaterial(baseColorFactor=F(1, 0, 0, 1)))
    red_only = centre(red)
    blue_opaque = centre(card(0.5, PBRMaterial(baseColorFactor=F(0, 0, 1, 1))))
    mixed = centre(red, card(0.5, PBRMaterial(baseColorFactor=F(0, 0, 1, 0.5), alphaMode="BLEND")))
    assert mixed[0] > 0 and mixed[2] > 0 and mixed[1] == 0
    expected = linear_to_srgb(0.5 * lin(blue_opaque) + 0.5 * lin(red_only)) * 255
    assert np.allclose(mixed, expected, atol=2)


def test_blend_alpha_zero_shows_behind() -> None:
    red = card(-0.5, PBRMaterial(baseColorFactor=F(1, 0, 0, 1)))
    clear = centre(red, card(0.5, PBRMaterial(baseColorFactor=F(0, 0, 1, 0), alphaMode="BLEND")))
    assert np.array_equal(clear, centre(red))


def test_blend_over_background_and_behind_opaque_hidden() -> None:
    blue = PBRMaterial(baseColorFactor=F(0, 0, 1, 0.5), alphaMode="BLEND")
    over_bg = centre(card(0, blue))
    assert 0 < over_bg[2] and over_bg[1] > 0  # background shows through
    red = card(0.5, PBRMaterial(baseColorFactor=F(1, 0, 0, 1)))
    assert np.array_equal(centre(red, card(-0.5, blue)), centre(red))


def test_only_nearest_blend_layer_contributes() -> None:
    red = card(-1, PBRMaterial(baseColorFactor=F(1, 0, 0, 1)))
    near = card(0.5, PBRMaterial(baseColorFactor=F(0, 0, 1, 0.5), alphaMode="BLEND"))
    far = card(0, PBRMaterial(baseColorFactor=F(0, 1, 0, 0.5), alphaMode="BLEND"))
    assert np.array_equal(centre(red, near, far), centre(red, near))


def test_mask_below_cutoff_shows_through() -> None:
    red = card(-0.5, PBRMaterial(baseColorFactor=F(1, 0, 0, 1)))
    m = card(0.5, PBRMaterial(baseColorFactor=F(0, 0, 1, 0.3), alphaMode="MASK", alphaCutoff=0.5))
    assert np.array_equal(centre(red, m), centre(red))
