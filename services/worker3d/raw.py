"""Raw TRELLIS.2 output <-> .npz bytes. Pickle-free, so re-export can load stored intermediates safely.

vertices (N, 3) f32 · faces (M, 3) i32 · attrs (L, C) f32 · coords (L, 3) i32 · voxel_size () f32 ·
layout: JSON {name: [start, stop]} over attrs channels.
"""
from __future__ import annotations

import io
import json
from typing import Any

import numpy as np
import torch

FORMAT = "assetstudio.trellis2-raw/1"


def dump(mesh: Any) -> bytes:
    def arr(t: Any, dtype: Any) -> np.ndarray:
        return (t.detach().cpu().numpy() if torch.is_tensor(t) else np.asarray(t)).astype(dtype)

    layout = {k: [s.start, s.stop] for k, s in mesh.layout.items()}
    buf = io.BytesIO()
    np.savez_compressed(buf, format=np.array(FORMAT), vertices=arr(mesh.vertices, np.float32),
                        faces=arr(mesh.faces, np.int32), attrs=arr(mesh.attrs, np.float32),
                        coords=arr(mesh.coords, np.int32), voxel_size=np.array(float(mesh.voxel_size), np.float32),
                        layout=np.array(json.dumps(layout)))
    return buf.getvalue()


def load(data: bytes) -> dict[str, Any]:
    with np.load(io.BytesIO(data), allow_pickle=False) as z:
        if str(z["format"]) != FORMAT:
            raise ValueError(f"unsupported raw format {z['format']}")
        layout = {k: slice(a, b) for k, (a, b) in json.loads(str(z["layout"])).items()}
        out = {k: torch.from_numpy(z[k]).cuda() for k in ("vertices", "attrs", "coords")}
        out["faces"] = torch.from_numpy(z["faces"]).cuda()
        out.update(voxel_size=float(z["voxel_size"]), layout=layout)
    if out["faces"].ndim != 2 or out["faces"].shape[1] != 3 or int(out["faces"].max()) >= out["vertices"].shape[0]:
        raise ValueError("raw mesh indices out of range")
    return out
