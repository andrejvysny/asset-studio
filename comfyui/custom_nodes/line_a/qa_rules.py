"""Combine VLM + mask checks into an advisory recommendation. Pure logic (unit-tested)."""
from __future__ import annotations

from typing import Any


def mask_checks(stats: dict[str, Any], rules: dict[str, Any]) -> dict[str, bool]:
    fill = rules["mask_fill_ratio"]
    return {
        "mask_not_cropped": not stats["touches_border"],
        "mask_fill_ratio": fill["min"] <= stats["fill_ratio"] <= fill["max"],
        "mask_single_blob": stats["components"] >= 1
        and stats["secondary_ratio"] < rules["mask_single_blob"]["min_secondary_ratio"],
    }


MASK_REASONS = {
    "mask_not_cropped": "Object touches the image border (likely cropped)",
    "mask_fill_ratio": "Object is too small or too large in frame",
    "mask_single_blob": "Cut-out mask has multiple separate objects",
}


def evaluate(
    candidate: str,
    vlm: dict[str, Any] | None,
    mask_stats: dict[str, Any] | None,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """Advisory only: returns recommended flag + reasons; never rejects."""
    checks: dict[str, bool] = {}
    severity: dict[str, str] = {}
    reasons: list[str] = []
    warnings: list[str] = []

    if vlm is not None:
        for cid, rule in rules["vlm_checks"].items():
            if cid in vlm.get("checks", {}):
                checks[cid] = bool(vlm["checks"][cid])
                severity[cid] = rule["severity"]
        reasons.extend(vlm.get("reasons", []))
        if vlm.get("missing_checks"):
            warnings.append(f"VLM skipped checks: {', '.join(vlm['missing_checks'])}")
    else:
        warnings.append("VLM QA unavailable")

    if mask_stats is not None:
        for cid, ok in mask_checks(mask_stats, rules["mask_checks"]).items():
            checks[cid] = ok
            severity[cid] = rules["mask_checks"][cid]["severity"]
            if not ok:
                reasons.append(MASK_REASONS[cid])
    else:
        warnings.append("mask QA unavailable")

    failed_major = [c for c, ok in checks.items() if not ok and severity[c] == "major"]
    failed_minor = [c for c, ok in checks.items() if not ok and severity[c] == "minor"]
    recommended = not failed_major and len(failed_minor) < rules["minor_fail_limit"]
    return {
        "candidate": candidate,
        "recommended": recommended,
        "status": "recommended" if recommended else "not_recommended",
        "checks": checks,
        "failed_major": failed_major,
        "failed_minor": failed_minor,
        "reasons": reasons,
        "warnings": warnings,
        "summary": (vlm or {}).get("summary", ""),
        "confidence": (vlm or {}).get("confidence", {}),
        "mask_stats": mask_stats,
    }
