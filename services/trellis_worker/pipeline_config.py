"""Rewrite TRELLIS.2 pipeline.json to local absolute paths so nothing is fetched from HF.

Upstream references microsoft/TRELLIS-image-large, facebook/dinov3 and briaai/RMBG-2.0 (gated,
CC BY-NC). We point rembg at the MIT ZhengPeng7/BiRefNet; it is never used anyway since we
always feed an RGBA cut-out.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

MODELS = Path(os.environ.get("MODELS_ROOT", "/models/checkpoints"))
TRELLIS_DIR = MODELS / "trellis2"
LOCAL_CFG_DIR = Path("/tmp/trellis2-local")


def _localize(ref: str) -> str:
    if ref.startswith("microsoft/TRELLIS-image-large/"):
        return str(MODELS / "trellis-image-large" / ref.removeprefix("microsoft/TRELLIS-image-large/"))
    return str(TRELLIS_DIR / ref)


def write_local_pipeline_json() -> Path:
    cfg = json.loads((TRELLIS_DIR / "pipeline.json").read_text())
    args = cfg["args"]
    args["models"] = {k: _localize(v) for k, v in args["models"].items()}
    args["image_cond_model"]["args"]["model_name"] = str(MODELS / "dinov3-vitl16")
    args["rembg_model"]["args"]["model_name"] = str(MODELS / "birefnet")
    LOCAL_CFG_DIR.mkdir(parents=True, exist_ok=True)
    (LOCAL_CFG_DIR / "pipeline.json").write_text(json.dumps(cfg, indent=2))
    return LOCAL_CFG_DIR
