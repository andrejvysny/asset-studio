"""Deterministic QA metrics (registered in assetstudio_core.qa.METRICS). CPU only: numpy + scipy."""
from __future__ import annotations

import io
from typing import Any

import numpy as np
from assetstudio_core.qa import CheckResult, QaRule
from PIL import Image
from scipy import ndimage

EVALUATOR = "assetstudio_processing.metrics/1"


def mask_array(mask_png: bytes) -> np.ndarray:
    """(H, W) uint8 alpha from a mask PNG (L or RGBA)."""
    with Image.open(io.BytesIO(mask_png)) as im:
        return np.asarray(im.getchannel("A") if im.mode == "RGBA" else im.convert("L"))


def mask_stats(mask: np.ndarray, alpha_threshold: int = 128, border_px: int = 4) -> dict[str, Any]:
    binary = mask >= alpha_threshold
    h, w = binary.shape
    total = int(binary.sum())
    b = max(1, border_px)
    border = np.concatenate([binary[:b].ravel(), binary[-b:].ravel(), binary[:, :b].ravel(), binary[:, -b:].ravel()])
    labels, n = ndimage.label(binary, structure=np.ones((3, 3), dtype=bool))
    areas = sorted((int(a) for a in np.bincount(labels.ravel())[1:]), reverse=True) if n else []
    ys, xs = np.nonzero(binary)
    return {
        "fill_ratio": round(total / (h * w), 4) if h * w else 0.0,
        "touches_border": bool(border.any()),
        "components": len(areas),
        "secondary_ratio": round(areas[1] / areas[0], 4) if len(areas) > 1 else 0.0,
        "bbox_xyxy": [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())] if total else None,
        "size": [w, h],
    }


def _result(rule: QaRule, ok: bool, observed: Any, threshold: Any, reason: str = "") -> CheckResult:
    return CheckResult(rule_id=rule.id, source=rule.source, severity=rule.severity, result="pass" if ok else "fail",
                       observed=observed, threshold=threshold, reason=reason, evaluator=EVALUATOR)


def unavailable(rule: QaRule, reason: str) -> CheckResult:
    return CheckResult(rule_id=rule.id, source=rule.source, severity=rule.severity, result="unavailable",
                       reason=reason, evaluator=EVALUATOR)


def not_applicable(rule: QaRule, reason: str) -> CheckResult:
    return CheckResult(rule_id=rule.id, source=rule.source, severity=rule.severity, result="not_applicable",
                       reason=reason, evaluator=EVALUATOR)


def _srgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    c = rgb.astype(np.float64) / 255.0
    c = np.where(c > 0.04045, ((c + 0.055) / 1.055) ** 2.4, c / 12.92)
    m = np.array([[0.4124564, 0.3575761, 0.1804375], [0.2126729, 0.7151522, 0.0721750],
                  [0.0193339, 0.1191920, 0.9503041]])
    xyz = c @ m.T / np.array([0.95047, 1.0, 1.08883])
    f = np.where(xyz > 216 / 24389, np.cbrt(xyz), (24389 / 27 * xyz + 16) / 116)
    return np.stack([116 * f[..., 1] - 16, 500 * (f[..., 0] - f[..., 1]), 200 * (f[..., 1] - f[..., 2])], axis=-1)


def reserved_coverage(rgb: np.ndarray, colors: list[tuple[str, float]], mask: np.ndarray | None) -> dict[str, float]:
    """Fraction of (foreground) pixels within ΔE76 tolerance of each reserved colour."""
    lab = _srgb_to_lab(rgb.reshape(-1, 3))
    fg = mask.reshape(-1) >= 128 if mask is not None else np.ones(len(lab), dtype=bool)
    denom = max(int(fg.sum()), 1)
    out: dict[str, float] = {}
    for hex_color, tol in colors:
        target = _srgb_to_lab(np.array([[int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]], dtype=np.uint8))[0]
        near = np.linalg.norm(lab - target, axis=1) < tol
        out[hex_color] = round(float((near & fg).sum()) / denom, 5)
    return out


def evaluate_metric(rule: QaRule, *, image_size: tuple[int, int] | None, mask: np.ndarray | None,
                    rgb: np.ndarray | None = None, reserved: list[tuple[str, float]] | None = None) -> CheckResult:
    p = rule.metric_params()
    if rule.metric in ("mask_margin", "mask_fill", "mask_single_blob"):
        if mask is None:
            return unavailable(rule, "foreground mask unavailable")
        st = mask_stats(mask, int(p["alpha_threshold"]), int(p.get("border_px", 4)))
        if st["fill_ratio"] == 0:
            return _result(rule, False, st, None, "mask is empty")
        if rule.metric == "mask_margin":
            return _result(rule, not st["touches_border"], {"touches_border": st["touches_border"]},
                           {"border_px": p["border_px"]}, "subject touches the image border" * st["touches_border"])
        if rule.metric == "mask_fill":
            ok = p["min"] <= st["fill_ratio"] <= p["max"]
            return _result(rule, ok, st["fill_ratio"], [p["min"], p["max"]], "" if ok else "subject too small or large")
        ok = st["secondary_ratio"] < p["max_secondary_ratio"]
        return _result(rule, ok, st["secondary_ratio"], p["max_secondary_ratio"], "" if ok else "multiple blobs")
    if rule.metric == "min_resolution":
        if image_size is None:
            return unavailable(rule, "image size unknown")
        short = min(image_size)
        return _result(rule, short >= p["min_px"], short, p["min_px"])
    if rule.metric == "palette_reserved":
        if rgb is None:
            return unavailable(rule, "image unavailable")
        if not reserved:
            return not_applicable(rule, "no reserved colours apply to this scope")
        cov = reserved_coverage(rgb, reserved, mask)
        worst = max(cov.values())
        return _result(rule, worst <= p["max_coverage"], cov, p["max_coverage"],
                       "" if worst <= p["max_coverage"] else "reserved colour present")
    return unavailable(rule, f"metric {rule.metric} has no implementation")
