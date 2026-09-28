"""Combine VLM + mask checks into an advisory status with explicit coverage. Pure logic (unit-tested).

Status is advisory only (never rejects):
  not_recommended  a major check failed, or >= minor_fail_limit minor checks failed
  unverified       no failure observed, but some configured checks did not run
  recommended      every configured check ran and passed the policy
"""
from __future__ import annotations

from typing import Any

MASK_QUESTIONS = {
    "mask_not_cropped": "Object touches the image border (likely cropped)",
    "mask_fill_ratio": "Object is too small or too large in frame",
    "mask_single_blob": "Cut-out mask has multiple separate objects",
}


def mask_checks(stats: dict[str, Any], rules: dict[str, Any]) -> dict[str, bool]:
    fill = rules["mask_fill_ratio"]
    return {
        "mask_not_cropped": not stats["touches_border"],
        "mask_fill_ratio": fill["min"] <= stats["fill_ratio"] <= fill["max"],
        "mask_single_blob": stats["components"] >= 1
        and stats["secondary_ratio"] < rules["mask_single_blob"]["min_secondary_ratio"],
    }


def configured_checks(rules: dict[str, Any]) -> dict[str, dict[str, str]]:
    """check id -> {severity, question, source}"""
    out = {cid: {"severity": r["severity"], "question": r["q"], "source": "vlm"} for cid, r in rules["vlm_checks"].items()}
    for cid, r in rules["mask_checks"].items():
        out[cid] = {"severity": r["severity"], "question": MASK_QUESTIONS[cid], "source": "mask"}
    return out


def evaluate(
    candidate: str,
    vlm: dict[str, Any] | None,
    mask_stats: dict[str, Any] | None,
    rules: dict[str, Any],
    vlm_error: str | None = None,
    mask_error: str | None = None,
) -> dict[str, Any]:
    config = configured_checks(rules)
    checks: dict[str, bool] = {}
    warnings: list[str] = []

    if vlm is not None:
        for cid, spec in config.items():
            if spec["source"] == "vlm" and cid in vlm.get("checks", {}):
                checks[cid] = bool(vlm["checks"][cid])
    else:
        warnings.append(f"VLM QA unavailable{': ' + vlm_error if vlm_error else ''}")
    if mask_stats is not None:
        checks.update(mask_checks(mask_stats, rules["mask_checks"]))
    else:
        warnings.append(f"mask QA unavailable{': ' + mask_error if mask_error else ''}")

    missing = [cid for cid in config if cid not in checks]
    if missing and vlm is not None and mask_stats is not None:
        warnings.append(f"{len(missing)} configured checks did not run: {', '.join(missing)}")

    failed_major = [c for c, ok in checks.items() if not ok and config[c]["severity"] == "major"]
    failed_minor = [c for c, ok in checks.items() if not ok and config[c]["severity"] == "minor"]
    if failed_major or len(failed_minor) >= rules["minor_fail_limit"]:
        status = "not_recommended"
    elif missing:
        status = "unverified"
    else:
        status = "recommended"

    reasons = list((vlm or {}).get("reasons", []))
    vlm_failed = [c for c in failed_major + failed_minor if config[c]["source"] == "vlm"]
    if vlm_failed and not reasons:  # model gave no prose: derive from failed check ids
        reasons = [f"Failed: {config[c]['question']}" for c in vlm_failed]
    reasons += [MASK_QUESTIONS[c] for c in failed_major + failed_minor if config[c]["source"] == "mask"]

    return {
        "candidate": candidate,
        "recommended": status == "recommended",
        "status": status,
        "checks": checks,
        "coverage": {"ran": len(checks), "total": len(config), "missing": missing},
        "failed_major": failed_major,
        "failed_minor": failed_minor,
        "reasons": reasons,
        "warnings": warnings,
        "summary": (vlm or {}).get("summary", ""),
        "confidence": (vlm or {}).get("confidence", {}),
        "mask_stats": mask_stats,
        "services": {"vlm": "ok" if vlm is not None else (vlm_error or "unavailable"),
                     "mask": "ok" if mask_stats is not None else (mask_error or "unavailable")},
    }
