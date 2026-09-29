"""Build retry modes (design: Retry same settings / Resample · new seed / Change settings, rebuild).

  build, retry  resume the durable upstream checkpoints of the latest unfinished attempt (same settings)
  resample      a NEW seed: keeps the segmentation, never the sample checkpoint
  rebuild       changed settings: keeps segmentation and the sampled raw, unless `pipeline_type` changed (that
                changes what TRELLIS.2 samples, so it resamples)
Re-export from a stored raw is its own command (services.production.reexport)."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import BuildRun, Checkpoint, JobItem
from assetstudio_core.seeds import derive_seed

from ...services.records import build_key, load_job

MODES = ("build", "retry", "resample", "rebuild")
RESUMABLE_STAGES = ("segment", "sample")  # upstream of the export settings: valid for any rebuild of the approval


def _priors(ctx: Any, job_id: str, item: JobItem, approval_id: str) -> list[BuildRun]:
    """Earlier plain builds of this approval, newest first."""
    out = []
    for rid in reversed(item.build_runs):
        prior, _ = ctx.store.get_opt(build_key(job_id, rid), BuildRun)
        if prior is not None and prior.inputs.get("approval_id") == approval_id and prior.kind == "build":
            out.append(prior)
    return out


def _pick(priors: list[BuildRun], wanted: tuple[str, ...]) -> tuple[str | None, dict[str, Checkpoint]]:
    for prior in priors:
        keep = {k: v for k, v in prior.checkpoints.items() if k in wanted}
        if keep:
            return prior.id, keep
    return None, {}


def inherit(ctx: Any, job_id: str, item: JobItem, approval_id: str, mode: str,
            overrides: dict[str, Any]) -> tuple[str | None, dict[str, Checkpoint]]:
    """Durable upstream checkpoints the new run starts from (resumed_from, checkpoints)."""
    priors = _priors(ctx, job_id, item, approval_id)
    if mode == "resample":
        return _pick(priors, ("segment",))
    if mode == "rebuild":
        return _pick(priors, ("segment",) if "pipeline_type" in overrides else RESUMABLE_STAGES)
    for prior in priors:  # build / retry: only an UNFINISHED attempt is resumed
        if prior.status == "succeeded":
            return None, {}
        keep = {k: v for k, v in prior.checkpoints.items() if k in RESUMABLE_STAGES}
        if keep:
            return prior.id, keep
    return None, {}


def mode_inputs(ctx: Any, job_id: str, item: JobItem, mode: str, overrides: dict[str, Any],
                command_id: str) -> dict[str, Any]:
    """What the run records about how it was requested (history rows read these)."""
    out: dict[str, Any] = {"mode": mode}
    if mode == "resample":
        seed = derive_seed(load_job(ctx.store, job_id)[0].seed_family, item.id, "resample", command_id)
        out["seed_override"] = seed % 2**31  # the range the sampler accepts: recorded == effective
    if mode == "rebuild":
        out["overrides"] = overrides
    return out
