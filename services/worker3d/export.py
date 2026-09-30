"""GLB export from a raw TRELLIS.2 mesh + attribute volume.

Port of o_voxel.postprocess.to_glb (TRELLIS.2 @ 75fbf01, MIT, Copyright (c) Microsoft Corporation) with the
texture-space rasteriser made pluggable, so the default path ships no NVIDIA non-commercial code. Other steps
(cleanup, simplification, UV unwrap, BVH re-projection, trilinear attribute sampling, inpainting) are upstream.
"""
from __future__ import annotations

import io
import time
from collections.abc import Callable
from typing import Any

import cv2
import numpy as np
import torch
import trimesh
from PIL import Image
from rasterize import RASTERIZERS

AABB = ((-0.5, -0.5, -0.5), (0.5, 0.5, 0.5))
HOLE_PERIMETER = 3e-2  # upstream constant; recorded as a known limitation
Rasterizer = Callable[[torch.Tensor, torch.Tensor, int], tuple[torch.Tensor, torch.Tensor]]


def _clean_and_simplify(mesh: Any, target: int, remesh: bool, bvh: Any, aabb: torch.Tensor, grid: torch.Tensor,
                        verts: torch.Tensor, faces: torch.Tensor, small_components: str = "remove",
                        fill_holes: str = "upstream") -> None:
    """Cleanup policy applies to the non-remesh path only: remesh rebuilds the topology from scratch."""
    if remesh:
        import cumesh

        resolution = int(grid.max())
        scale = float((aabb[1] - aabb[0]).max())
        mesh.init(*cumesh.remeshing.remesh_narrow_band_dc(
            verts, faces, center=aabb.mean(dim=0), scale=(resolution + 3) / resolution * scale,
            resolution=resolution, band=1, project_back=0.0, verbose=False, bvh=bvh))
        mesh.simplify(target, verbose=False)
        return
    mesh.simplify(target * 3, verbose=False)
    for final in (False, True):
        mesh.remove_duplicate_faces()
        mesh.repair_non_manifold_edges()
        if small_components == "remove":
            mesh.remove_small_connected_components(1e-5)
        if fill_holes == "upstream":
            mesh.fill_holes(max_hole_perimeter=HOLE_PERIMETER)
        if not final:
            mesh.simplify(target, verbose=False)
    mesh.unify_face_orientations()


def _bake(raw: dict[str, Any], out_v: torch.Tensor, out_f: torch.Tensor, out_uv: torch.Tensor, size: int,
          rasterize: Rasterizer, bvh: Any, verts: torch.Tensor, faces: torch.Tensor,
          aabb: torch.Tensor, grid: torch.Tensor, voxel: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Texel -> surface point -> nearest point on the original mesh -> trilinear sample of the attribute volume."""
    from flex_gemm.ops.grid_sample import grid_sample_3d

    face_id, bary = rasterize(out_uv, out_f.long(), size)
    mask = face_id >= 0
    tri = out_v[out_f[face_id[mask]].long()]  # (N, 3, 3)
    pos = (tri * bary[mask].unsqueeze(-1)).sum(1)
    _, fid, uvw = bvh.unsigned_distance(pos, return_uvw=True)
    pos = (verts[faces[fid.long()]] * uvw.unsqueeze(-1)).sum(1)
    attrs = raw["attrs"]
    out = torch.zeros(size, size, attrs.shape[1], device="cuda")
    out[mask] = grid_sample_3d(attrs, torch.cat([torch.zeros_like(raw["coords"][:, :1]), raw["coords"]], -1),
                               shape=torch.Size([1, attrs.shape[1], *grid.tolist()]),
                               grid=((pos - aabb[0]) / voxel).reshape(1, -1, 3), mode="trilinear")
    return out, mask


def _material(attrs: torch.Tensor, mask: torch.Tensor, layout: dict[str, slice],
              double_sided: bool) -> trimesh.visual.material.PBRMaterial:
    holes = (~mask.cpu().numpy()).astype(np.uint8)

    def chan(name: str, radius: int) -> np.ndarray:
        a = np.clip(attrs[..., layout[name]].cpu().numpy() * 255, 0, 255).astype(np.uint8)
        a = cv2.inpaint(a, holes, radius, cv2.INPAINT_TELEA)  # dilate into gutters so UV seams don't bleed black
        return a if a.ndim == 3 else a[..., None]

    base, metal, rough, alpha = chan("base_color", 3), chan("metallic", 1), chan("roughness", 1), chan("alpha", 1)
    return trimesh.visual.material.PBRMaterial(
        baseColorTexture=Image.fromarray(np.concatenate([base, alpha], -1)),
        baseColorFactor=np.array([255, 255, 255, 255], dtype=np.uint8),
        metallicRoughnessTexture=Image.fromarray(np.concatenate([np.zeros_like(metal), rough, metal], -1)),
        metallicFactor=1.0, roughnessFactor=1.0, alphaMode="OPAQUE", doubleSided=double_sided)


def to_glb(raw: dict[str, Any], exporter: str, decimation_target: int, texture_size: int,
           remesh: bool, small_components: str = "remove",
           fill_holes: str = "upstream") -> tuple[bytes, dict[str, Any]]:
    import cumesh

    t0 = time.monotonic()
    aabb = torch.tensor(AABB, dtype=torch.float32, device="cuda")
    voxel = torch.tensor([raw["voxel_size"]] * 3, dtype=torch.float32, device="cuda")
    grid = ((aabb[1] - aabb[0]) / voxel).round().int()
    mesh = cumesh.CuMesh()
    mesh.init(raw["vertices"], raw["faces"])
    if fill_holes == "upstream":
        mesh.fill_holes(max_hole_perimeter=HOLE_PERIMETER)
    verts, faces = mesh.read()
    bvh = cumesh.cuBVH(verts, faces)
    _clean_and_simplify(mesh, decimation_target, remesh, bvh, aabb, grid, verts, faces, small_components, fill_holes)
    out_v, out_f, out_uv, vmaps = mesh.uv_unwrap(
        compute_charts_kwargs={"threshold_cone_half_angle_rad": np.radians(90.0), "refine_iterations": 0,
                               "global_iterations": 1, "smooth_strength": 1}, return_vmaps=True, verbose=False)
    out_v, out_f, out_uv, vmaps = out_v.cuda(), out_f.cuda(), out_uv.cuda(), vmaps.cuda()
    mesh.compute_vertex_normals()
    normals = mesh.read_vertex_normals()[vmaps]
    t1 = time.monotonic()
    attrs, mask = _bake(raw, out_v, out_f, out_uv, texture_size, RASTERIZERS[exporter], bvh, verts, faces,
                        aabb, grid, voxel)
    t2 = time.monotonic()
    material = _material(attrs, mask, raw["layout"], double_sided=not remesh)
    v, n, uv = out_v.cpu().numpy(), normals.cpu().numpy(), out_uv.cpu().numpy()
    v[:, 1], v[:, 2] = v[:, 2], -v[:, 1].copy()  # TRELLIS z-up -> glTF y-up (upstream conversion)
    n[:, 1], n[:, 2] = n[:, 2], -n[:, 1].copy()
    uv[:, 1] = 1 - uv[:, 1]
    tm = trimesh.Trimesh(vertices=v, faces=out_f.cpu().numpy(), vertex_normals=n, process=False,
                         visual=trimesh.visual.TextureVisuals(uv=uv, material=material))
    buf = io.BytesIO()
    tm.export(buf, file_type="glb")
    return buf.getvalue(), {
        "exporter": exporter, "faces_in": int(raw["faces"].shape[0]), "faces_out": int(out_f.shape[0]),
        "vertices_out": int(out_v.shape[0]), "texture_size": texture_size, "texel_coverage": float(mask.float().mean()),
        "decimation_target": decimation_target, "remesh": remesh,
        "geometry_policy": {"small_components": small_components, "fill_holes": fill_holes, "applies": not remesh},
        "timings_s": {"geometry": round(t1 - t0, 2), "bake": round(t2 - t1, 2),
                      "total": round(time.monotonic() - t0, 2)},
    }
