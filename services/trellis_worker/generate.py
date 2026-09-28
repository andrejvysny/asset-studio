"""TRELLIS.2 sampling, raw-intermediate persistence and GLB export (GPU side)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
import trimesh
from PIL import Image

HDRI = Path("/opt/TRELLIS.2/assets/hdri/studio.exr")
RAW_FIELDS = ("vertices", "faces", "attrs", "coords", "layout", "voxel_size")
# Upstream o_voxel.to_glb always fills small holes (max perimeter 3e-2) and removes tiny components;
# neither is switchable without patching upstream. Recorded on every attempt.
KNOWN_LIMITATIONS = ["to_glb always fills holes with perimeter < 0.03 (upstream, not switchable)",
                     "texture bake uses nvdiffrast v0.4.0 (NVIDIA non-commercial licence)"]


def run_trellis(pipe: object, cutout: Image.Image, seed: int, pipeline_type: str) -> object:
    torch.manual_seed(seed)
    mesh = pipe.run(cutout, seed=seed, pipeline_type=pipeline_type)[0]
    mesh.simplify(16777216)  # nvdiffrast limit, per upstream example
    return mesh


def save_raw(mesh: object, path: Path) -> None:
    """Everything to_glb needs, so export can be retried without resampling TRELLIS."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    for f in RAW_FIELDS:
        v = getattr(mesh, f)
        data[f] = v.detach().cpu() if torch.is_tensor(v) else v
    torch.save(data, path)


def load_raw(path: Path) -> SimpleNamespace:
    data = torch.load(path, map_location="cpu", weights_only=False)
    return SimpleNamespace(**{k: v.cuda() if torch.is_tensor(v) else v for k, v in data.items()})


def export_glb(mesh: object, decimation_target: int, texture_size: int, remesh: bool) -> trimesh.Trimesh:
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
        remesh=remesh,
        remesh_band=1,
        remesh_project=0,
        verbose=True,
    )


def render_preview(mesh: object, out_path: Path) -> None:
    """2x2 shaded snapshot of the raw TRELLIS mesh (pre-export). Best effort; labelled as such by the caller."""
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
