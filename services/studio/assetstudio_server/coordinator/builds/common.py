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
from assetstudio_processing.raster import to_png

from ...adapters.base import EngineRejected
from ...services.records import build_key, load_decision, load_item, load_qa, mutate_item
from ..errors import Blocked, ItemFailed
from ..runner import TaskEnv
from .modes import inherit, mode_inputs


class BuildFailed(ItemFailed):
    """Unrecoverable problem for this item's build (not a structural-validation failure)."""


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


def foreground_mask(inp: BuildInput, allow_compute: bool = False) -> np.ndarray:
    """Mask for the approved bytes: this run's segment checkpoint, else the QA mask when it was computed from
    exactly these bytes, else (GPU1 segment stage only) segment now."""
    store = inp.env.ctx.store
    if (cp := inp.done("segment")) is not None and cp.outputs.get("mask"):
        inp.meta["mask"] = cp.identities.get("mask") or {"artifact_id": cp.outputs["mask"], "source": "segment"}
        return metrics.mask_array(store.artifact_bytes(cp.outputs["mask"]))
    mask_id = reusable_qa_mask(inp.env.ctx, inp.batch_id, inp.bound, inp.source)
    if mask_id:
        inp.meta["mask"] = {"artifact_id": mask_id, "source": "qa_reused"}
        return metrics.mask_array(store.artifact_bytes(mask_id))
    if not allow_compute:
        raise ItemFailed("no reusable foreground mask and no segmentation stage ran for this build", "internal")
    aux = inp.env.studio.aux
    if aux is None:
        raise Blocked("no segmentation service configured", "aux_unconfigured", operator=True)
    try:
        res = aux.cutout(image=inp.data, epoch=inp.env.epoch("aux"))
    except EngineRejected as e:
        raise ItemFailed(f"segmentation rejected the approved image: {e}", "input_invalid") from e
    art = store.register_artifact(res["mask_png"], "mask", "image/png", lineage=[inp.source.id],
                                  source={"model": res.get("meta", {})}, artifact_id=inp.artifact_id("mask"))
    inp.meta["mask"] = {"artifact_id": art.id, "source": "computed", "simulated": bool(aux.simulated)}
    return metrics.mask_array(res["mask_png"])


def reusable_qa_mask(ctx: Any, job_id: str, bound: dict[str, Any], source: Artifact) -> str | None:
    """The QA mask artifact id when it was computed from exactly the approved candidate bytes."""
    qa_id = bound.get("qa_evaluation_id")
    mask_id = load_qa(ctx.store, job_id, qa_id).evaluators.get("mask_artifact_id") if qa_id else None
    if not mask_id:
        return None
    mask_art = ctx.store.artifact(mask_id)
    ok = mask_art.lineage == [source.id] and source.sha256 == bound["image_sha256"]
    return mask_id if ok else None


def create_run(studio: Any, ctx: Any, job_id: str, item: JobItem, approval_id: str, build: str, command_id: str,
               reexport_from: str | None = None, overrides: dict[str, Any] | None = None,
               mode: str = "build") -> BuildRun:
    """At command time: the run exists and is attached to the item BEFORE any stage runs (H01)."""
    store = ctx.store
    run_id = derived_id("run", command_id, item.id)
    existing, _ = store.get_opt(build_key(job_id, run_id), BuildRun)
    if existing is None:
        bound = load_decision(store, job_id, approval_id).bound
        now = now_iso()
        run = BuildRun(id=run_id, item_id=item.id, job_id=job_id, build=build,
                       inputs={"approval_id": approval_id, "candidate_artifact_id": bound["artifact_id"],
                               "image_sha256": bound["image_sha256"]},
                       status="queued", created_at=now, updated_at=now, op_id=command_id,
                       kind="reexport" if reexport_from else "build", derived_from=reexport_from)
        if reexport_from:
            run.inputs = {**run.inputs, "reexport_of": reexport_from, "overrides": overrides or {},
                          "mode": "reexport"}
        else:
            resumed_from, checkpoints = inherit(ctx, job_id, item, approval_id, mode, overrides or {})
            run.inputs = {**run.inputs, **mode_inputs(ctx, job_id, item, mode, overrides or {}, command_id)}
            if checkpoints:
                run.checkpoints = checkpoints
                run.artifacts = {k: v for cp in checkpoints.values() for k, v in cp.outputs.items()}
                run.inputs = {**run.inputs, "resumed_from": resumed_from}
        store.create_or_same(build_key(job_id, run_id), run)
        existing = run

    def attach(x: JobItem) -> None:
        if run_id not in x.build_runs:
            x.build_runs.append(run_id)
        x.current_build = run_id
    if run_id not in item.build_runs or item.current_build != run_id:
        mutate_item(studio, ctx, job_id, item.id, attach)
    return existing


def open_input(env: TaskEnv) -> BuildInput:
    """BuildInput for the build stage task `env.task` (inputs: build_run_id)."""
    t, store = env.task, env.ctx.store
    run, token = store.get(build_key(t.job_id, t.inputs["build_run_id"]), BuildRun)
    item, _ = load_item(store, t.job_id, t.item_id)
    bound = load_decision(store, t.job_id, run.inputs["approval_id"]).bound
    source = store.artifact(bound["artifact_id"])
    snap = store.read_snapshot(item.snapshot_sha)
    reexport = None
    if run.derived_from and run.kind == "reexport":
        prior = store.get(build_key(t.job_id, run.derived_from), BuildRun)[0]
        reexport = {"from_run": prior, "overrides": run.inputs.get("overrides", {})}
    inp = BuildInput(env, t.job_id, item, bound, source, store.artifact_bytes(source.id),
                     {**snap["parameters"], **run.inputs.get("overrides", {})}, snap, reexport,
                     roles=dict(run.artifacts), run=run, token=token)
    if run.status not in ("running",):
        run.status, run.error = "running", None
        inp.save()
    return inp


def finish_run(inp: BuildInput, build: str) -> dict[str, Any]:
    """Required checks decide validity; advisory ones are reported. Preview is rendered after, isolated."""
    assert inp.run is not None
    inp.check("hash_matches_approval", inp.source.sha256 == inp.bound["image_sha256"])
    if inp.meta:
        inp.add_json("meta", {"build": build, **inp.meta})
    required = [c for c in inp.checks if not c.get("advisory")]
    ok = all(c["ok"] for c in required)
    if inp.components:
        inp.run.inputs = {**inp.run.inputs, "components": inp.components}
    inp.run.validation = {"ok": ok, "checks": inp.checks, "required": [c["id"] for c in required]}
    inp.run.status, inp.run.result = "succeeded", "valid" if ok else "invalid"
    inp.save()
    if ok and inp.preview is not None:
        render_preview(inp)
    return {"build_run_id": inp.run.id, "result": inp.run.result}


def mark_run(env: TaskEnv, state: str, err: dict[str, Any]) -> None:
    """Stage error hook: the run records the outcome and keeps every durable artifact/checkpoint."""
    t, store = env.task, env.ctx.store
    run, token = store.get(build_key(t.job_id, t.inputs["build_run_id"]), BuildRun)
    if run.status == "succeeded":
        return
    run.status = {"failed": "failed", "cancelled": "cancelled"}.get(state, "blocked")  # type: ignore[assignment]
    run.error = f"{t.stage}: {err.get('message', '')}"[:300]
    run.validation = {**run.validation, "failure_code": err.get("code"), "failed_stage": t.stage}
    run.updated_at = now_iso()
    store.replace(build_key(t.job_id, run.id), run, token)
def render_preview(inp: BuildInput) -> None:
    """A preview is a derivative of the delivered file: failure leaves the valid model available (retryable)."""
    assert inp.run is not None and inp.preview is not None
    try:
        png = inp.preview()
        model = inp.roles.get("model")
        base = model or inp.roles.get("image")
        derived = "CPU render, 4 views, simplified shading" if model else "thumbnail of the delivered image"
        art = inp.env.ctx.store.register_artifact(png, "preview", "image/png", lineage=[base] if base else [],
                                                  meta={"derived": derived},
                                                  artifact_id=inp.artifact_id("preview"))
        inp.roles["preview"] = art.id
        inp.run.preview, inp.run.preview_error = "available", None
    except Exception as e:  # any renderer problem: record, never invalidate the model
        inp.run.preview, inp.run.preview_error = "failed", f"{type(e).__name__}: {e}"[:300]
    inp.save()
