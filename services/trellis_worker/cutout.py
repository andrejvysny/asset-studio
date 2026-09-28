"""BiRefNet background removal + deterministic mask stats used by QA and cut-out validation."""
from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import torch
from PIL import Image
from torchvision import transforms

BIREFNET_DIR = Path(os.environ.get("MODELS_ROOT", "/models/checkpoints")) / "birefnet"

_TRANSFORM = transforms.Compose(
    [
        transforms.Resize((1024, 1024)),
        transforms.ToTensor(),
        transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
    ]
)


def load_birefnet() -> torch.nn.Module:
    from transformers import AutoModelForImageSegmentation

    model = AutoModelForImageSegmentation.from_pretrained(
        str(BIREFNET_DIR), trust_remote_code=True, local_files_only=True
    )
    return model.eval().half().cuda()


def predict_mask(model: torch.nn.Module, image: Image.Image) -> np.ndarray:
    """Returns uint8 alpha mask (H, W) at the input resolution."""
    rgb = image.convert("RGB")
    x = _TRANSFORM(rgb).unsqueeze(0).cuda().half()
    with torch.inference_mode():
        pred = model(x)[-1].sigmoid()[0, 0].float().cpu().numpy()
    return cv2.resize((pred * 255).astype(np.uint8), rgb.size, interpolation=cv2.INTER_LINEAR)


def mask_stats(mask: np.ndarray, alpha_threshold: int = 128, border_px: int = 4) -> dict:
    binary = (mask >= alpha_threshold).astype(np.uint8)
    h, w = binary.shape
    total = int(binary.sum())
    border = np.concatenate(
        [binary[:border_px].ravel(), binary[-border_px:].ravel(), binary[:, :border_px].ravel(), binary[:, -border_px:].ravel()]
    )
    n, _, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    areas = sorted((int(a) for a in stats[1:, cv2.CC_STAT_AREA]), reverse=True)
    ys, xs = np.nonzero(binary)
    bbox = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if total else None
    return {
        "fill_ratio": round(total / (h * w), 4),
        "touches_border": bool(border.any()),
        "components": len(areas),
        "secondary_ratio": round(areas[1] / areas[0], 4) if len(areas) > 1 else 0.0,
        "bbox_xyxy": bbox,
        "size": [w, h],
    }


def write_cutout(image: Image.Image, mask: np.ndarray, rgba_path: Path, mask_path: Path | None) -> None:
    rgba = image.convert("RGB").copy()
    rgba.putalpha(Image.fromarray(mask))
    rgba_path.parent.mkdir(parents=True, exist_ok=True)
    rgba.save(rgba_path)
    if mask_path is not None:
        mask_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(mask).save(mask_path)
