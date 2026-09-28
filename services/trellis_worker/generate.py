"""TRELLIS.2 generation, GLB export, safe cleanup and mesh stats."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import torch
import trimesh
from PIL import Image

HDRI = Path("/opt/TRELLIS.2/assets/hdri/studio.exr")


def run_trellis(pipe: object, cutout: Image.Image, seed: int, pipeline_type: str) -> object:
    torch.manual_seed(seed)
    mesh = pipe.run(cutout, seed=seed, pipeline_type=pipeline_type)[0]
    mesh.simplify(16777216)  # nvdiffrast limit, per upstream example
    return mesh


def export_glb(mesh: object, decimation_target: int, texture_size: int) -> trimesh.Trimesh:
    """Hole filling, tiny-component removal and decimation all happen in to_glb before UV/bake."""
    import o_voxel

    return o_voxel.postprocess.to_glb(
        vertices=mesh.vertices,
        faces=mesh.faces,
        attr_volume=mesh.attrs,
        coords=mesh.coords,
        attr_layout=mesh.layout,
        voxel_size=mesh.voxel_size,
        aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        decimation_target=decimation_target,
        texture_size=texture_size,
        remesh=True,
        remesh_band=1,
        remesh_project=0,
        verbose=True,
    )


def drop_floaters(mesh: trimesh.Trimesh, min_ratio: float) -> tuple[trimesh.Trimesh, int]:
    """Drop whole components smaller than min_ratio of faces. Keeps UVs of the remaining faces intact."""
    labels = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(mesh.faces))
    counts = np.bincount(labels)
    keep_labels = np.nonzero(counts >= min_ratio * len(mesh.faces))[0]
    if len(keep_labels) == len(counts):
        return mesh, 0
    keep = np.isin(labels, keep_labels)
    mesh.update_faces(keep)
    mesh.remove_unreferenced_vertices()
    return mesh, int(len(counts) - len(keep_labels))


def mesh_info(mesh: trimesh.Trimesh, glb_path: Path, removed_components: int) -> dict:
    labels = trimesh.graph.connected_component_labels(mesh.face_adjacency, node_count=len(mesh.faces))
    material = getattr(mesh.visual, "material", None)
    return {
        "triangles": int(len(mesh.faces)),
        "vertices": int(len(mesh.vertices)),
        "components": int(labels.max() + 1) if len(labels) else 0,
        "removed_floater_components": removed_components,
        "bbox_min": mesh.bounds[0].round(5).tolist(),
        "bbox_max": mesh.bounds[1].round(5).tolist(),
        "extents": mesh.extents.round(5).tolist(),
        "watertight": bool(mesh.is_watertight),
        "has_uv": getattr(mesh.visual, "uv", None) is not None,
        "has_base_color_texture": getattr(material, "baseColorTexture", None) is not None,
        "has_metallic_roughness_texture": getattr(material, "metallicRoughnessTexture", None) is not None,
        "file_size_bytes": glb_path.stat().st_size,
    }


def save_textures(mesh: trimesh.Trimesh, out_dir: Path) -> list[str]:
    material = getattr(mesh.visual, "material", None)
    saved: list[str] = []
    for name in ("baseColorTexture", "metallicRoughnessTexture"):
        tex = getattr(material, name, None)
        if isinstance(tex, Image.Image):
            out_dir.mkdir(parents=True, exist_ok=True)
            tex.save(out_dir / f"{name}.png")
            saved.append(f"{name}.png")
    return saved


def render_preview(mesh: object, out_path: Path) -> None:
    """2x2 shaded turntable snapshot. Best effort; caller treats failure as non-fatal."""
    import cv2
    from trellis2.renderers import EnvMap
    from trellis2.utils import render_utils

    hdri = cv2.cvtColor(cv2.imread(str(HDRI), cv2.IMREAD_UNCHANGED), cv2.COLOR_BGR2RGB)
    envmap = EnvMap(torch.tensor(hdri, dtype=torch.float32, device="cuda"))
    frames = render_utils.render_snapshot(mesh, resolution=512, bg_color=(1, 1, 1), nviews=4, r=2, fov=40, envmap=envmap)
    views = frames.get("shaded") or frames.get("color")
    grid = np.concatenate([np.concatenate(views[:2], axis=1), np.concatenate(views[2:4], axis=1)], axis=0)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(grid).save(out_path)


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2) + "\n")
