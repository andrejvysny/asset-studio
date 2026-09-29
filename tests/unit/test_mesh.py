"""Mesh fixtures from the review: UV-split connected surface, legit small parts, true floaters, empty."""
from pathlib import Path

import numpy as np
import pytest
import trimesh
from assetstudio_processing import mesh as meshcheck
from PIL import Image


def uv_split_plane(n: int = 11) -> trimesh.Trimesh:
    """One connected plane where every quad is its own UV chart (vertices duplicated at seams)."""
    verts, faces = [], []
    for i in range(n):
        for j in range(n):
            base = len(verts)
            verts += [[i, j, 0], [i + 1, j, 0], [i + 1, j + 1, 0], [i, j + 1, 0]]
            faces += [[base, base + 1, base + 2], [base, base + 2, base + 3]]
    return trimesh.Trimesh(vertices=np.array(verts, float), faces=np.array(faces), process=False)


def test_uv_split_surface_is_one_component_and_survives_cleanup() -> None:
    mesh = uv_split_plane()
    assert len(mesh.faces) == 242
    assert meshcheck.geometric_components(mesh).max() == 0
    cleaned, removed = meshcheck.drop_floaters(mesh, 0.01)
    assert removed == 0 and len(cleaned.faces) == 242


def test_true_floater_removed_largest_kept() -> None:
    body = trimesh.creation.icosphere(subdivisions=3)
    speck = trimesh.creation.icosphere(subdivisions=0, radius=0.01)
    speck.apply_translation([3, 0, 0])
    mesh = trimesh.util.concatenate([body, speck])
    cleaned, removed = meshcheck.drop_floaters(mesh, 0.05)
    assert removed == 1 and len(cleaned.faces) == len(body.faces)


def test_cleanup_never_empties_mesh() -> None:
    parts = [trimesh.creation.box().apply_translation([i * 3, 0, 0]) for i in range(200)]
    cleaned, _ = meshcheck.drop_floaters(trimesh.util.concatenate(parts), 0.5)
    assert len(cleaned.faces) > 0


def test_empty_mesh_rejected(tmp_path: Path) -> None:
    empty = trimesh.Trimesh(vertices=np.zeros((0, 3)), faces=np.zeros((0, 3), int), process=False)
    with pytest.raises(ValueError):
        meshcheck.drop_floaters(empty, 0.01)


def _textured_box(tmp_path: Path) -> Path:
    box = trimesh.creation.box()
    uv = np.random.default_rng(0).random((len(box.vertices), 2))
    mat = trimesh.visual.material.PBRMaterial(baseColorTexture=Image.new("RGB", (8, 8), (200, 100, 50)))
    box.visual = trimesh.visual.TextureVisuals(uv=uv, material=mat)
    path = tmp_path / "box.glb"
    box.export(path)
    return path


def test_validate_textured_glb_ok(tmp_path: Path) -> None:
    v = meshcheck.validate_glb(_textured_box(tmp_path))
    assert v["ok"], v


def test_validate_untextured_glb_fails(tmp_path: Path) -> None:
    path = tmp_path / "plain.glb"
    trimesh.creation.box().export(path)
    v = meshcheck.validate_glb(path)
    assert not v["ok"] and {c["id"] for c in v["checks"] if not c["ok"]} >= {"uvs_present", "material_texture_present"}


def test_validate_garbage_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.glb"
    path.write_bytes(b"not a glb")
    assert not meshcheck.validate_glb(path)["ok"]
