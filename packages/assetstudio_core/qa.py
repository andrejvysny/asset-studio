"""Advisory QA: versioned rule sets, allowlisted metrics, strict result parsing, recommendation policy.

Status is advisory only (the user may approve anything with an explicit override):
  not_recommended  any major failure, or >= minor_fail_limit minor failures
  unverified       no such failure, but an applicable enabled check could not run (or none were applicable)
  recommended      every applicable enabled check completed and the policy passed
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .ids import validate_config_key
from .kinds import Kind

Source = Literal["vlm", "image_metric", "mask_metric", "mesh_metric", "frame_metric"]
Stage = Literal["candidate", "build"]
Result = Literal["pass", "fail", "unavailable", "not_applicable"]
Status = Literal["recommended", "not_recommended", "unverified"]


@dataclass(frozen=True)
class MetricSpec:
    source: Source
    description: str
    params: dict[str, float | int]
    requires: tuple[str, ...] = field(default=())


# Registered deterministic metrics. A UI "Metric" picks one of these + typed params; never code.
METRICS: dict[str, MetricSpec] = {
    "mask_margin": MetricSpec("mask_metric", "foreground keeps a margin from the image border",
                              {"border_px": 4, "alpha_threshold": 128}, ("mask",)),
    "mask_fill": MetricSpec("mask_metric", "foreground fill ratio within range",
                            {"min": 0.08, "max": 0.75, "alpha_threshold": 128}, ("mask",)),
    "mask_single_blob": MetricSpec("mask_metric", "secondary foreground blobs below ratio",
                                   {"max_secondary_ratio": 0.02, "alpha_threshold": 128}, ("mask",)),
    "min_resolution": MetricSpec("image_metric", "image at least this many pixels on the short side",
                                 {"min_px": 512}),
    "palette_reserved": MetricSpec("image_metric", "reserved palette colours absent outside allowed scope",
                                   {"max_coverage": 0.01}, ("palette",)),
}


class QaRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    source: Source
    stage: Stage = "candidate"
    enabled: bool = True
    severity: Literal["major", "minor"] = "minor"
    question: str | None = Field(default=None, max_length=500)
    metric: str | None = None
    params: dict[str, float | int | bool | str] = {}

    @field_validator("id")
    @classmethod
    def _key(cls, v: str) -> str:
        return validate_config_key(v)

    @model_validator(mode="after")
    def _shape(self) -> QaRule:
        if self.source == "vlm":
            if not self.question:
                raise ValueError("a VLM rule needs a question")
            if self.metric:
                raise ValueError("a VLM rule must not name a metric")
        else:
            if self.metric not in METRICS:
                raise ValueError(f"unknown metric {self.metric!r}; registered: {sorted(METRICS)}")
            spec = METRICS[self.metric]
            if spec.source != self.source:
                raise ValueError(f"metric {self.metric} is a {spec.source} rule")
            unknown = set(self.params) - set(spec.params)
            if unknown:
                raise ValueError(f"unknown params for {self.metric}: {sorted(unknown)}")
        return self

    def metric_params(self) -> dict[str, Any]:
        assert self.metric is not None
        return {**METRICS[self.metric].params, **self.params}


class QaPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minor_fail_limit: int = Field(default=2, ge=1)


class QaRuleset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = ""
    kind: Kind | None = None
    rules: list[QaRule] = []
    policy: QaPolicy = QaPolicy()

    @model_validator(mode="after")
    def _unique(self) -> QaRuleset:
        ids = [r.id for r in self.rules]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate rule ids: {sorted(dupes)}")
        return self


class CheckResult(BaseModel):
    rule_id: str
    source: Source | Literal["vlm_compare"]  # vlm_compare: multi-image comparison (variant/reference QA)
    severity: Literal["major", "minor"]
    result: Result
    reason: str = ""
    observed: Any = None
    threshold: Any = None
    evaluator: str = ""


def parse_vlm_answers(answers: Any, rule_ids: list[str]) -> dict[str, bool | str]:
    """Strict: only real JSON booleans count. Anything else becomes an explanation string (-> unavailable)."""
    if not isinstance(answers, dict):
        return {rid: "VLM returned no checks object" for rid in rule_ids}
    out: dict[str, bool | str] = {}
    for rid in rule_ids:
        if rid not in answers:
            out[rid] = "VLM did not answer this check"
        elif isinstance(answers[rid], bool):
            out[rid] = answers[rid]
        else:
            out[rid] = f"non-boolean answer {answers[rid]!r}"
    return out


def evaluate_policy(rules: list[QaRule], results: list[CheckResult], policy: QaPolicy) -> dict[str, Any]:
    """Pure aggregation. Disabled rules are listed but never counted; zero applicable -> unverified."""
    by_id = {r.rule_id: r for r in results}
    enabled = [r for r in rules if r.enabled]
    applicable = [r for r in enabled if by_id.get(r.id) is None or by_id[r.id].result != "not_applicable"]
    completed = [r for r in applicable if r.id in by_id and by_id[r.id].result in ("pass", "fail")]
    failed_major = [r.id for r in completed if by_id[r.id].result == "fail" and r.severity == "major"]
    failed_minor = [r.id for r in completed if by_id[r.id].result == "fail" and r.severity == "minor"]
    unavailable = [r.id for r in applicable if r not in completed]

    status: Status
    if failed_major or len(failed_minor) >= policy.minor_fail_limit:
        status = "not_recommended"
    elif unavailable or not applicable:
        status = "unverified"
    else:
        status = "recommended"
    return {
        "status": status,
        "not_evaluated": not applicable,
        "coverage": {"completed": len(completed), "applicable": len(applicable)},
        "failed_major": failed_major,
        "failed_minor": failed_minor,
        "unavailable": unavailable,
        "disabled": [r.id for r in rules if not r.enabled],
        "minor_fail_limit": policy.minor_fail_limit,
    }


def recommendation_rank(evaluation: dict[str, Any]) -> tuple[int, int]:
    """Deterministic bulk-approval ordering among recommended candidates: fewer minor failures first."""
    return (len(evaluation.get("failed_minor", [])), len(evaluation.get("unavailable", [])))
