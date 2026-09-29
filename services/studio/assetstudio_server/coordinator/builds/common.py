"""Shared build-run scaffolding: BuildRun attached at creation, per-stage checkpoints, resumable worker executions,
bound-input checks, mask reuse, derived artifacts, preview as a separately retryable derivative."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from assetstudio_core.canonical import canonical_json, now_iso
from assetstudio_core.domain import Artifact, BuildRun, Checkpoint, JobItem
from assetstudio_core.ids import derived_id
from assetstudio_processing import metrics
from assetstudio_processing.images import ImageRejected
from assetstudio_processing.raster import RasterError, to_png

from ...adapters.base import EngineRejected, EngineUnavailable
from ...services.records import build_key, load_decision, load_qa, mutate_item
from ..runner import Blocked, Cancelled, TaskEnv


class BuildFailed(Exception):
    """Unrecoverable input problem for this item (not a structural-validation failure)."""

    def __init__(self, message: str, code: str = "build_failed") -> None:
        super().__init__(message)
        self.code = code


@dataclass
class BuildInput:
    env: TaskEnv
    batch_id: str
    item: JobItem
    bound: dict[str, Any]
    source: Artifact
    data: bytes
    params: dict[str, Any]
    snap: dict[str, Any] = field(default_factory=dict)
    reexport: dict[str, Any] | None = None  # {"from_run": BuildRun, "overrides": {...}}: reuse stored raw output
    roles: dict[str, str] = field(default_factory=dict)
    checks: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    components: list[str] = field(default_factory=list)  # licence-table ids of what produced the outputs
    run: BuildRun | None = None
    token: str | None = None
    preview: Callable[[], bytes] | None = None  # derivative rendered after validation; its failure is isolated

    def check(self, check_id: str, ok: bool, detail: str = "", advisory: bool = False) -> bool:
        """Advisory checks are reported but never make a build invalid (e.g. advisory triangle budgets)."""
        self.checks.append({"id": check_id, "ok": bool(ok), **({"detail": detail} if detail else {}),
                            **({"advisory": True} if advisory else {})})
        return bool(ok)

    def artifact_id(self, role: str, *extra: str) -> str:
        """Derived per (run, role): re-registering after a crash returns the same artifact record."""
        assert self.run is not None
        return derived_id("art", self.run.id, role, *extra)

    def add_png(self, role: str, arr: np.ndarray, meta: dict[str, Any] | None = None) -> str:
        art = self.env.ctx.store.register_artifact(to_png(arr), role, "image/png", lineage=[self.source.id],
                                                   meta={"width": int(arr.shape[1]), "height": int(arr.shape[0]),
                                                         **(meta or {})}, artifact_id=self.artifact_id(role))
        self.roles[role] = art.id
        return art.id

    def add_json(self, role: str, doc: dict[str, Any]) -> str:
        art = self.env.ctx.store.register_artifact(canonical_json(doc), role, "application/json",
                                                   lineage=[self.source.id], artifact_id=self.artifact_id(role))
        self.roles[role] = art.id
        return art.id

    def save(self) -> None:
        """Persist the run (conditional replace on its storage token)."""
        assert self.run is not None and self.token is not None
        self.run.artifacts = {**self.run.artifacts, **self.roles}
        self.run.updated_at = now_iso()
        self.token = self.env.ctx.store.replace(build_key(self.batch_id, self.run.id), self.run, self.token)

    def checkpoint(self, stage: str, outputs: dict[str, str], *, inputs: dict[str, Any] | None = None,
                   settings: dict[str, Any] | None = None, identities: dict[str, Any] | None = None,
                   receipt: dict[str, Any] | None = None) -> None:
        assert self.run is not None
        self.roles.update(outputs)
        self.run.checkpoints[stage] = Checkpoint(stage=stage, inputs=inputs or {}, settings=settings or {},
                                                 identities=identities or {}, outputs=outputs,
                                                 receipt=receipt or {}, committed_at=now_iso())
        self.save()

    def done(self, stage: str) -> Checkpoint | None:
        return self.run.checkpoints.get(stage) if self.run else None

    def execution_id(self, stage: str) -> str:
        """The id this stage's worker execution uses, persisted BEFORE submission. One execution per stage per run:
        a lost/failed execution fails the run; a new attempt is an explicit new run (which inherits checkpoints)."""
        assert self.run is not None
        ids = self.run.executions.setdefault(stage, [])
        if ids:
            return ids[-1]
        eid = f"{self.run.id}-{stage}-1"
        ids.append(eid)
        self.save()
        return eid


BuildFn = Callable[[BuildInput], None]


def foreground_mask(inp: BuildInput) -> np.ndarray:
    """Reuse the QA mask when it was computed from exactly the approved bytes; otherwise segment now (GPU1)."""
    store = inp.env.ctx.store
    qa_id = inp.bound.get("qa_evaluation_id")
    mask_id = load_qa(store, inp.batch_id, qa_id).evaluators.get("mask_artifact_id") if qa_id else None
    if mask_id:
        mask_art = store.artifact(mask_id)
        if mask_art.lineage == [inp.source.id] and inp.source.sha256 == inp.bound["image_sha256"]:
            inp.meta["mask"] = {"artifact_id": mask_id, "source": "qa_reused"}
            return metrics.mask_array(store.artifact_bytes(mask_id))
    aux = inp.env.studio.aux
    if aux is None:
        raise BuildFailed("no segmentation service configured", "resource_unavailable")
    epoch = inp.env.studio.lanes["gpu1"].acquire("aux")
    try:
        res = aux.cutout(image=inp.data, epoch=epoch)
    except (EngineUnavailable, EngineRejected) as e:
        raise BuildFailed(f"segmentation unavailable: {e}", "resource_unavailable") from e
    art = store.register_artifact(res["mask_png"], "mask", "image/png", lineage=[inp.source.id],
                                  source={"model": res.get("meta", {})}, artifact_id=inp.artifact_id("mask"))
    inp.meta["mask"] = {"artifact_id": art.id, "source": "computed", "simulated": bool(aux.simulated)}
    return metrics.mask_array(res["mask_png"])


RESUMABLE_STAGES = ("segment", "sample")  # upstream of the export settings: valid for any rebuild of the approval


def _inherited(env: TaskEnv, batch_id: str, item: JobItem, approval_id: str) -> tuple[str | None, dict[str, Any]]:
    """Durable upstream checkpoints of the latest unfinished attempt for the same approval (e.g. a raw from a run
    whose bake failed): an explicit rebuild resumes from them instead of resampling."""
    for rid in reversed(item.build_runs):
        prior, _ = env.ctx.store.get_opt(build_key(batch_id, rid), BuildRun)
        if prior is None or prior.inputs.get("approval_id") != approval_id or prior.kind != "build":
            continue
        if prior.status == "succeeded":
            return None, {}
        keep = {k: v for k, v in prior.checkpoints.items() if k in RESUMABLE_STAGES}
        if keep:
            return prior.id, keep
    return None, {}


def _open_run(env: TaskEnv, batch_id: str, item: JobItem, entry: dict[str, Any], build: str,
              bound: dict[str, Any]) -> tuple[BuildRun, str]:
    """Create (or find) the run and attach it to the item's history NOW, so a failed attempt stays visible."""
    store = env.ctx.store
    run_id = derived_id("run", env.op.id, item.id)
    existing, token = store.get_opt(build_key(batch_id, run_id), BuildRun)
    if existing is None:
        now = now_iso()
        run = BuildRun(id=run_id, item_id=item.id, job_id=batch_id, build=build,
                       inputs={"approval_id": entry["approval_id"], "candidate_artifact_id": bound["artifact_id"],
                               "image_sha256": bound["image_sha256"]},
                       status="running", created_at=now, updated_at=now, op_id=env.op.id,
                       kind="reexport" if entry.get("reexport_from") else "build",
                       derived_from=entry.get("reexport_from"))
        if entry.get("reexport_from"):
            run.inputs = {**run.inputs, "reexport_of": entry["reexport_from"], "overrides": entry.get("overrides", {})}
        else:
            resumed_from, checkpoints = _inherited(env, batch_id, item, entry["approval_id"])
            if checkpoints:
                run.checkpoints = checkpoints
                run.artifacts = {k: v for cp in checkpoints.values() for k, v in cp.outputs.items()}
                run.inputs = {**run.inputs, "resumed_from": resumed_from}
        token = store.create(build_key(batch_id, run_id), run)
        existing = run
    assert token is not None

    def attach(x: JobItem) -> None:
        if run_id not in x.build_runs:
            x.build_runs.append(run_id)
        x.current_build = run_id
    if run_id not in item.build_runs or item.current_build != run_id:
        mutate_item(env.studio, env.ctx, batch_id, item.id, attach)
    return existing, token


def _fail(inp: BuildInput, message: str, code: str) -> None:
    """Keep every already registered artifact (e.g. a durable raw) on the failed run before exposing failure."""
    assert inp.run is not None
    inp.run.status, inp.run.error = "failed", message[:300]
    inp.run.validation = {**inp.run.validation, "failure_code": code}
    inp.save()


def run_build(env: TaskEnv, batch_id: str, item: JobItem, entry: dict[str, Any], build: str,
              fn: BuildFn) -> BuildRun:
    """One BuildRun per (op, item). A finished run returns unchanged; an interrupted/failed one resumes from its
    committed checkpoints (a durable raw is never regenerated)."""
    store = env.ctx.store
    bound = load_decision(store, batch_id, entry["approval_id"]).bound
    run, token = _open_run(env, batch_id, item, entry, build, bound)
    if run.status == "succeeded":
        return run
    source = store.artifact(bound["artifact_id"])
    snap = store.read_snapshot(item.snapshot_sha)
    reexport = None
    if entry.get("reexport_from"):
        prior = store.get(build_key(batch_id, entry["reexport_from"]), BuildRun)[0]
        reexport = {"from_run": prior, "overrides": entry.get("overrides", {})}
    run.status, run.error = "running", None
    inp = BuildInput(env, batch_id, item, bound, source, store.artifact_bytes(source.id),
                     {**snap["parameters"], **(reexport or {}).get("overrides", {})}, snap, reexport,
                     roles=dict(run.artifacts), run=run, token=token)
    inp.save()
    if inp.check("hash_matches_approval", source.sha256 == bound["image_sha256"]):
        try:
            fn(inp)
        except (RasterError, ImageRejected) as e:
            inp.check("derive", False, str(e)[:300])
        except BuildFailed as e:
            _fail(inp, str(e), e.code)
            raise
        except Cancelled:
            inp.run.status, inp.run.error = "cancelled", "cancelled by operator"
            inp.save()
            raise
        except Blocked as e:  # resource unavailable: checkpoints stay; the next attempt resumes
            inp.run.status, inp.run.error = "blocked", str(e)[:300]
            inp.save()
            raise
        except Exception as e:
            _fail(inp, f"{type(e).__name__}: {e}", "internal")
            raise
    if inp.meta:
        inp.add_json("meta", {"build": build, **inp.meta})
    required = [c for c in inp.checks if not c.get("advisory")]
    ok = all(c["ok"] for c in required)
    if inp.components:
        run.inputs = {**run.inputs, "components": inp.components}
    run.validation = {"ok": ok, "checks": inp.checks, "required": [c["id"] for c in required]}
    run.status, run.result = "succeeded", "valid" if ok else "invalid"
    inp.save()
    if ok and inp.preview is not None:
        render_preview(inp)
    return run


def render_preview(inp: BuildInput) -> None:
    """A preview is a derivative of the delivered file: failure leaves the valid model available (retryable)."""
    assert inp.run is not None and inp.preview is not None
    try:
        png = inp.preview()
        model = inp.roles.get("model")
        art = inp.env.ctx.store.register_artifact(png, "preview", "image/png", lineage=[model] if model else [],
                                                  meta={"derived": "CPU render, 4 views, simplified shading"},
                                                  artifact_id=inp.artifact_id("preview"))
        inp.roles["preview"] = art.id
        inp.run.preview, inp.run.preview_error = "available", None
    except Exception as e:  # any renderer problem: record, never invalidate the model
        inp.run.preview, inp.run.preview_error = "failed", f"{type(e).__name__}: {e}"[:300]
    inp.save()
