"""Shared build-run scaffolding: idempotent BuildRun records, bound-input checks, mask reuse, derived artifacts."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from assetstudio_core.canonical import canonical_json, now_iso
from assetstudio_core.domain import Artifact, BatchItem, BuildRun
from assetstudio_core.ids import derived_id
from assetstudio_processing import metrics
from assetstudio_processing.images import ImageRejected
from assetstudio_processing.raster import RasterError, to_png

from ...adapters.base import EngineRejected, EngineUnavailable
from ...services.records import build_key, load_decision, load_qa
from ..runner import TaskEnv


class BuildFailed(Exception):
    """Unrecoverable input problem for this item (not a structural-validation failure)."""


@dataclass
class BuildInput:
    env: TaskEnv
    batch_id: str
    item: BatchItem
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

    def check(self, check_id: str, ok: bool, detail: str = "", advisory: bool = False) -> bool:
        """Advisory checks are reported but never make a build invalid (e.g. advisory triangle budgets)."""
        self.checks.append({"id": check_id, "ok": bool(ok), **({"detail": detail} if detail else {}),
                            **({"advisory": True} if advisory else {})})
        return bool(ok)

    def add_png(self, role: str, arr: np.ndarray, meta: dict[str, Any] | None = None) -> str:
        art = self.env.ctx.store.register_artifact(to_png(arr), role, "image/png", lineage=[self.source.id],
                                                   meta={"width": int(arr.shape[1]), "height": int(arr.shape[0]),
                                                         **(meta or {})})
        self.roles[role] = art.id
        return art.id

    def add_json(self, role: str, doc: dict[str, Any]) -> str:
        art = self.env.ctx.store.register_artifact(canonical_json(doc), role, "application/json",
                                                   lineage=[self.source.id])
        self.roles[role] = art.id
        return art.id


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
        raise BuildFailed("no segmentation service configured")
    inp.env.studio.lanes["gpu1"].acquire("aux")
    try:
        res = aux.cutout(image=inp.data)
    except (EngineUnavailable, EngineRejected) as e:
        raise BuildFailed(f"segmentation unavailable: {e}") from e
    art = store.register_artifact(res["mask_png"], "mask", "image/png", lineage=[inp.source.id],
                                  source={"model": res.get("meta", {})})
    inp.meta["mask"] = {"artifact_id": art.id, "source": "computed", "simulated": bool(aux.simulated)}
    return metrics.mask_array(res["mask_png"])


def _open_run(env: TaskEnv, batch_id: str, item: BatchItem, approval_id: str, build: str,
              bound: dict[str, Any]) -> tuple[BuildRun, str]:
    store = env.ctx.store
    run_id = derived_id("run", env.op.id, item.id)
    existing, token = store.get_opt(build_key(batch_id, run_id), BuildRun)
    if existing is not None:
        assert token is not None
        return existing, token
    now = now_iso()
    run = BuildRun(id=run_id, item_id=item.id, batch_id=batch_id, build=build,
                   inputs={"approval_id": approval_id, "candidate_artifact_id": bound["artifact_id"],
                           "image_sha256": bound["image_sha256"]},
                   status="running", created_at=now, updated_at=now, op_id=env.op.id)
    return run, store.create(build_key(batch_id, run_id), run)


def run_build(env: TaskEnv, batch_id: str, item: BatchItem, entry: dict[str, Any], build: str,
              fn: BuildFn) -> BuildRun:
    """One BuildRun per (op, item); re-running a finished op returns the recorded result unchanged."""
    store = env.ctx.store
    approval_id = entry["approval_id"]
    bound = load_decision(store, batch_id, approval_id).bound
    run, token = _open_run(env, batch_id, item, approval_id, build, bound)
    if run.status == "succeeded":
        return run
    source = store.artifact(bound["artifact_id"])
    snap = store.read_snapshot(item.snapshot_sha)
    reexport = None
    if entry.get("reexport_from"):
        prior = store.get(build_key(batch_id, entry["reexport_from"]), BuildRun)[0]
        reexport = {"from_run": prior, "overrides": entry.get("overrides", {})}
        run.inputs = {**run.inputs, "reexport_of": prior.id, "overrides": entry.get("overrides", {})}
    inp = BuildInput(env, batch_id, item, bound, source, store.artifact_bytes(source.id),
                     {**snap["parameters"], **(reexport or {}).get("overrides", {})}, snap, reexport)
    if inp.check("hash_matches_approval", source.sha256 == bound["image_sha256"]):
        try:
            fn(inp)
        except (RasterError, ImageRejected) as e:
            inp.check("derive", False, str(e)[:300])
        except BuildFailed as e:
            run.status, run.error, run.updated_at = "failed", str(e)[:300], now_iso()
            store.replace(build_key(batch_id, run.id), run, token)
            raise
    if inp.meta:
        inp.add_json("meta", {"build": build, **inp.meta})
    required = [c for c in inp.checks if not c.get("advisory")]
    ok = all(c["ok"] for c in required)
    run.artifacts = inp.roles
    if inp.components:
        run.inputs = {**run.inputs, "components": inp.components}
    run.validation = {"ok": ok, "checks": inp.checks, "required": [c["id"] for c in required]}
    run.status, run.result, run.updated_at = "succeeded", "valid" if ok else "invalid", now_iso()
    store.replace(build_key(batch_id, run.id), run, token)
    return run
