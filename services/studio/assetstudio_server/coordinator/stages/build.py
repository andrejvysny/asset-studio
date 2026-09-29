"""Build + publication stages. Builds read only the bound approval; publication commits exact accepted runs."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import AssetManifest, BuildRun, JobItem, Published
from assetstudio_core.kinds import Kind, Origin
from assetstudio_processing.images import ImageRejected
from assetstudio_processing.raster import RasterError
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, StalePointer, publish
from assetstudio_storage.repo import Conflict, IntegrityError, StorageError

from ...models import load_lock
from ...provenance import licence_summary
from ...services.records import build_key, load_cset, load_decision, load_item, load_prompt, load_qa, mutate_item
from ..builds import model3d as m3d
from ..builds.common import BuildInput, finish_run, foreground_mask, open_input, render_preview
from ..builds.kinds import BUILDS
from ..errors import ItemFailed
from ..runner import TaskEnv

# Kept on the build run (re-export, provenance) but not shipped as version files: raw TRELLIS output is ~100-300 MB.
INTERMEDIATE_ROLES = ("raw", "cutout", "mask")


def segment(env: TaskEnv) -> dict[str, Any]:
    inp = open_input(env)
    if inp.run.build == "model3d":  # type: ignore[union-attr]
        return {"cutout": m3d.segment(inp)}
    if (cp := inp.done("segment")) is None:
        foreground_mask(inp, allow_compute=True)
        mask = inp.meta.get("mask", {})
        inp.checkpoint("segment", {"mask": mask["artifact_id"]}, inputs={"source_sha256": inp.source.sha256},
                       identities={"mask": mask})
        cp = inp.done("segment")
    return {"mask": cp.outputs["mask"]}  # type: ignore[union-attr]


def sample(env: TaskEnv) -> dict[str, Any]:
    return {"raw": m3d.sample(open_input(env))}


def bake(env: TaskEnv) -> dict[str, Any]:
    return {"model": m3d.bake(open_input(env))}


def finalize(env: TaskEnv) -> dict[str, Any]:
    inp = open_input(env)
    m3d.finalize(inp)
    return finish_run(inp, "model3d")


def derive(env: TaskEnv) -> dict[str, Any]:
    """CPU derivation of image kinds (canvas, variants, passthrough, material checks)."""
    inp = open_input(env)
    assert inp.run is not None
    fn = BUILDS.get(inp.run.build)
    if fn is None:
        raise ItemFailed(f"no build implementation {inp.run.build}", "unsupported_configuration")
    try:
        fn(inp)
    except (RasterError, ImageRejected) as e:
        inp.check("derive", False, str(e)[:300])
    return finish_run(inp, inp.run.build)


def preview(env: TaskEnv) -> dict[str, Any]:
    """Preview-only retry: never regenerates or rebakes; renders from the delivered model bytes."""
    inp = open_input(env)
    assert inp.run is not None
    model = inp.run.artifacts.get("model")
    if model is None:
        raise ItemFailed("this run has no model to preview", "input_invalid")
    from assetstudio_processing.render import preview_png

    data = env.ctx.store.artifact_bytes(model)
    inp.preview = lambda: preview_png(data)
    inp.run.status = "succeeded"
    render_preview(inp)
    return {"preview": inp.run.preview}


def _details(env: TaskEnv, job_id: str, item: JobItem, run: BuildRun) -> dict[str, Any]:
    store = env.ctx.store
    decision = load_decision(store, job_id, run.inputs["approval_id"])
    b = decision.bound
    cset = load_cset(store, job_id, b["candidate_set_id"])
    qa = load_qa(store, job_id, b["qa_evaluation_id"]) if b.get("qa_evaluation_id") else None
    prompt = load_prompt(store, job_id, b["prompt_revision_id"])
    lock = load_lock(env.studio.settings.config_dir)
    used = cset.generation.get("models", [])
    models = [{"key": k, "repo": lock["models"][k]["repo"], "revision": lock["models"][k]["revision"],
               "files": lock["models"][k]["files"]} for k in used if k in lock["models"]]
    return {
        "sources": {"job_id": job_id, "item_id": item.id, "shot_id": item.shot_id, "prompt_revision_id": prompt.id,
                    "candidate_set_id": cset.id, "candidate_id": b["candidate_id"], "approval_id": decision.id,
                    "build_run_id": run.id, "positive": prompt.positive, "negative": prompt.negative,
                    "intermediates": {k: v for k, v in run.artifacts.items() if k in INTERMEDIATE_ROLES},
                    "checkpoints": {k: {"identities": c.identities, "settings": c.settings}
                                    for k, c in run.checkpoints.items()}},
        "config_snapshot_sha": item.snapshot_sha,
        "models": models,
        "engine": {k: v for k, v in cset.generation.items() if k not in ("models",)},
        "parameters": {"seed": b.get("seed"), **cset.generation.get("params", {})},
        "qa": None if qa is None else {"evaluation_id": qa.id, "status": qa.policy["status"],
                                       "coverage": qa.policy["coverage"], "override": decision.override_qa,
                                       "override_reason": decision.override_reason,
                                       "failed": decision.failed_checks, "missing": decision.missing_checks},
        "validation": run.validation,
        "licence": licence_summary(env.studio.settings.config_dir, used, cset.generation,
                                   run.inputs.get("components")),
    }


def publish_item(env: TaskEnv) -> dict[str, Any]:
    t, store = env.task, env.ctx.store
    e = t.inputs
    item, _ = load_item(store, t.job_id, t.item_id)
    run, _ = store.get(build_key(t.job_id, e["build_run_id"]), BuildRun)
    if item.accepted_build != run.id or run.result != "valid":
        raise ItemFailed("only the accepted, valid build result can be published", "stale_input")
    snap = store.read_snapshot(item.snapshot_sha)
    kind = Kind(snap["recipe"]["kind"])
    req = PublishRequest(
        op_id=f"{t.id}", idempotency_key=t.command_id,
        artifacts={k: v for k, v in run.artifacts.items() if k not in INTERMEDIATE_ROLES},
        preview_role="preview" if "preview" in run.artifacts else None, origin=Origin.generated, kind=kind,
        asset_id=e["asset_id"],
        new_asset=None if e["asset_id"] else NewAsset(e["name_id"], item.name, kind, Origin.generated,
                                                     item.category_id),
        expected_current_version=e.get("expected_current_version"), make_current=e["make_current"],
        details=_details(env, t.job_id, item, run))
    try:
        res = publish(store, req)
    except StalePointer as err:
        raise ItemFailed(str(err)[:300], "stale_pointer") from err
    except (Conflict, IntegrityError, StorageError) as err:
        raise ItemFailed(str(err)[:300], getattr(err, "code", "conflict")) from err
    env.ctx.index.upsert(store.get(manifest_key(res.asset_id), AssetManifest)[0])

    def apply(x: JobItem) -> None:
        x.published = Published(asset_id=res.asset_id, version_id=res.version_id,
                                display_version=res.display_version)
        if x.target_asset_id is None:
            x.target_asset_id = res.asset_id
    mutate_item(env.studio, env.ctx, t.job_id, item.id, apply)
    env.studio.events.publish("library", project_id=env.ctx.id)
    return {"asset_id": res.asset_id, "version_id": res.version_id, "display_version": res.display_version}


__all__ = ["BuildInput", "bake", "derive", "finalize", "preview", "publish_item", "sample", "segment"]
