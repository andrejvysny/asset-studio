"""Texture-space rasterisers for GLB baking: which triangle covers each texel centre, with barycentric weights.

Both follow nvdiffrast's convention so the exporters differ only here: texel (row r, col c) samples
uv = ((c + 0.5) / S, (r + 0.5) / S); no conservative rasterisation; later triangles win on shared edges.

Shapes: uvs (V, 2) in [0, 1]; faces (F, 3) int64; returns face_id (S, S) int64 (-1 = empty) and bary (S, S, 3).
"""
from __future__ import annotations

import torch

CANDIDATE_BUDGET = 1 << 25  # texel candidates per step (~32M) bounds peak memory, even for one huge triangle


def _segments(lo: torch.Tensor, wh: torch.Tensor) -> list[tuple[int, int, int, int]]:
    """(first face, end face, row offset, row count) steps. Consecutive small faces share a step; a face whose
    bounding box alone exceeds the budget is split into row tiles instead of allocating it whole."""
    counts = (wh[:, 0] * wh[:, 1]).tolist()
    widths = wh[:, 0].tolist()
    heights = wh[:, 1].tolist()
    out: list[tuple[int, int, int, int]] = []
    start, acc = 0, 0
    for i, n in enumerate(counts):
        if n > CANDIDATE_BUDGET:
            if start < i:
                out.append((start, i, 0, -1))
            rows = max(1, CANDIDATE_BUDGET // max(1, widths[i]))
            out += [(i, i + 1, r, min(rows, heights[i] - r)) for r in range(0, heights[i], rows)]
            start, acc = i + 1, 0
            continue
        if acc + n > CANDIDATE_BUDGET and start < i:
            out.append((start, i, 0, -1))
            start, acc = i, 0
        acc += n
    if start < len(counts):
        out.append((start, len(counts), 0, -1))
    return out


def rasterize_uv_torch(uvs: torch.Tensor, faces: torch.Tensor, size: int) -> tuple[torch.Tensor, torch.Tensor]:
    dev = uvs.device
    tri = uvs.float()[faces] * size - 0.5  # (F, 3, 2) in texel-centre units
    lo = tri.amin(1).ceil().clamp(0, size - 1).long()
    hi = tri.amax(1).floor().clamp(0, size - 1).long()
    wh = (hi - lo + 1).clamp(min=0)  # empty when the triangle covers no texel centre
    face_id = torch.full((size * size,), -1, dtype=torch.long, device=dev)
    bary = torch.zeros((size * size, 3), dtype=torch.float32, device=dev)
    for a, b, row0, rows in _segments(lo, wh):
        lo_s, wh_s = lo[a:b].clone(), wh[a:b].clone()
        if rows >= 0:  # one tiled face: restrict its box to this row band
            lo_s[:, 1] += row0
            wh_s[:, 1] = rows
        _raster_step(tri[a:b], lo_s, wh_s, a, size, face_id, bary)
    return face_id.view(size, size), bary.view(size, size, 3)


def _raster_step(tri: torch.Tensor, lo: torch.Tensor, wh: torch.Tensor, offset: int, size: int,
                 face_id: torch.Tensor, bary: torch.Tensor) -> None:
    """Winner per texel = highest face index covering it (nvdiffrast convention: later faces win), decided by an
    order-independent amax, and the barycentrics are gathered from exactly that winner: face id and weights are
    always correlated, whatever the step order or duplicate texels (plain duplicate-index writes are not)."""
    counts = wh[:, 0] * wh[:, 1]
    total = int(counts.sum())
    if total == 0:
        return
    dev = tri.device
    idx = torch.repeat_interleave(torch.arange(len(tri), device=dev), counts)
    local = torch.arange(total, device=dev) - (torch.cumsum(counts, 0) - counts)[idx]
    px = lo[idx, 0] + local % wh[idx, 0]
    py = lo[idx, 1] + local // wh[idx, 0]
    a, b, c = tri[idx, 0], tri[idx, 1], tri[idx, 2]
    v0, v1 = b - a, c - a
    v2 = torch.stack([px.float(), py.float()], -1) - a
    den = v0[:, 0] * v1[:, 1] - v1[:, 0] * v0[:, 1]
    safe = torch.where(den.abs() > 1e-12, den, torch.ones_like(den))
    l1 = (v2[:, 0] * v1[:, 1] - v1[:, 0] * v2[:, 1]) / safe
    l2 = (v0[:, 0] * v2[:, 1] - v2[:, 0] * v0[:, 1]) / safe
    l0 = 1 - l1 - l2
    eps = -1e-6
    inside = (den.abs() > 1e-12) & (l0 >= eps) & (l1 >= eps) & (l2 >= eps)  # degenerate faces never win
    lin = (py * size + px)[inside]
    fid = idx[inside] + offset
    face_id.scatter_reduce_(0, lin, fid, reduce="amax", include_self=True)
    won = face_id[lin] == fid  # unique: a face contributes at most one candidate per texel
    bary[lin[won]] = torch.stack([l0, l1, l2], -1)[inside][won]


def rasterize_uv_nvdiffrast(uvs: torch.Tensor, faces: torch.Tensor, size: int) -> tuple[torch.Tensor, torch.Tensor]:
    """Upstream o_voxel.to_glb path (NVIDIA non-commercial licence), same output contract as the torch version."""
    import nvdiffrast.torch as dr

    ctx = dr.RasterizeCudaContext()
    clip = torch.cat([uvs * 2 - 1, torch.zeros_like(uvs[:, :1]), torch.ones_like(uvs[:, :1])], -1).unsqueeze(0)
    faces32 = faces.int()
    rast = torch.zeros((1, size, size, 4), device=uvs.device, dtype=torch.float32)
    for i in range(0, faces32.shape[0], 100000):
        chunk, _ = dr.rasterize(ctx, clip, faces32[i:i + 100000], resolution=[size, size])
        hit = chunk[..., 3:4] > 0
        chunk[..., 3:4] += i
        rast = torch.where(hit, chunk, rast)
    r = rast[0]
    face_id = torch.where(r[..., 3] > 0, r[..., 3].long() - 1, torch.full_like(r[..., 3], -1).long())
    u, v = r[..., 0], r[..., 1]
    return face_id, torch.stack([u, v, 1 - u - v], -1)


def available() -> dict[str, bool]:
    try:
        import nvdiffrast

        research = not getattr(nvdiffrast, "STUB", False)
    except ImportError:
        research = False
    return {"clean": True, "research": research}


RASTERIZERS = {"clean": rasterize_uv_torch, "research": rasterize_uv_nvdiffrast}
