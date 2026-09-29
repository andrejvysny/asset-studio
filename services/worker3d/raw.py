"""Raw TRELLIS.2 output <-> .npz bytes. Pickle-free; loading validates schema and limits on CPU (raw_npz) BEFORE
any tensor reaches the GPU.
"""
from __future__ import annotations

import io
import json
from typing import Any

import numpy as np
import raw_npz
import torch

FORMAT = raw_npz.FORMAT
RawInvalid = raw_npz.RawInvalid


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


def validate(data: bytes) -> dict[str, Any]:
    """CPU only. Raises RawInvalid."""
    return raw_npz.load(data)


def to_cuda(raw: dict[str, Any]) -> dict[str, Any]:
    out = {k: torch.from_numpy(raw[k]).cuda() for k in ("vertices", "attrs", "coords", "faces")}
    out.update(voxel_size=raw["voxel_size"], layout=raw["layout"])
    return out
