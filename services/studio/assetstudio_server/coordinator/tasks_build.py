"""Build and publish passes (CPU). Builds use only the bound approval; publication is per asset, idempotent."""
from __future__ import annotations

from typing import Any

from assetstudio_core.domain import AssetManifest, BatchItem, BuildRun, Published
from assetstudio_core.kinds import Kind, Origin
from assetstudio_core.recipes import RECIPES
from assetstudio_storage.project import manifest_key
from assetstudio_storage.publication import NewAsset, PublishRequest, StalePointer, publish
from assetstudio_storage.repo import Conflict, IntegrityError

from ..models import load_lock
from ..provenance import licence_summary
from ..services.records import (
    load_batch,
    load_build,
    load_cset,
    load_decision,
    load_item,
    load_prompt,
    load_qa,
    mutate_item,
    set_task,
)
from .builds import BUILDS, BuildFailed, run_build
from .runner import TaskEnv
from .tasks_prompt import _items_error


def build(env: TaskEnv) -> dict[str, Any]:
    batch_id = env.op.payload["batch_id"]
    batch, _ = load_batch(env.ctx.store, batch_id)
    recipe = RECIPES[batch.recipe_id]
    out = {}
    for entry in env.op.payload["items"]:
        env.check_cancel()
        item, _ = load_item(env.ctx.store, batch_id, entry["item_id"])
        if item.approval != entry["approval_id"]:
            mutate_item(env.studio, env.ctx, batch_id, item.id,
                        lambda x: set_task(x, "build", env.op.id, "failed", "approval changed before build"))
            out[item.id] = "stale"
            continue
        mutate_item(env.studio, env.ctx, batch_id, item.id, lambda x: set_task(x, "build", env.op.id, "running"))
        fn = BUILDS.get(recipe.build or "")
        if fn is None:
            raise RuntimeError(f"no build implementation {recipe.build}")
        try:
            run = run_build(env, batch_id, item, entry["approval_id"], recipe.build or "", fn)
        except BuildFailed as e:
            mutate_item(env.studio, env.ctx, batch_id, item.id,
                        lambda x, e=e: set_task(x, "build", env.op.id, "failed", str(e)[:300]))
            out[item.id] = "failed"
            continue

        def apply(x: BatchItem, run: BuildRun = run) -> None:
            if run.id not in x.build_runs:
                x.build_runs.append(run.id)
            x.current_build = run.id
            set_task(x, "build", env.op.id, "succeeded" if run.result == "valid" else "failed",
                     None if run.result == "valid" else "structural validation failed")
        mutate_item(env.studio, env.ctx, batch_id, item.id, apply)
        out[item.id] = run.result or "unknown"
    return {"items": out}


def build_error(env: TaskEnv, state: str, message: str) -> None:
    _items_error(env, "build", state, message, [e["item_id"] for e in env.op.payload["items"]])


def _details(env: TaskEnv, batch_id: str, item: BatchItem, run: BuildRun) -> dict[str, Any]:
    store = env.ctx.store
    decision = load_decision(store, batch_id, run.inputs["approval_id"])
    b = decision.bound
    cset = load_cset(store, batch_id, b["candidate_set_id"])
    qa = load_qa(store, batch_id, b["qa_evaluation_id"]) if b.get("qa_evaluation_id") else None
    prompt = load_prompt(store, batch_id, b["prompt_revision_id"])
    lock = load_lock(env.studio.settings.config_dir)
    used = cset.generation.get("models", [])
    models = [{"key": k, "repo": lock["models"][k]["repo"], "revision": lock["models"][k]["revision"],
               "files": lock["models"][k]["files"]} for k in used if k in lock["models"]]
    return {
        "sources": {"batch_id": batch_id, "item_id": item.id, "shot_id": item.shot_id, "prompt_revision_id": prompt.id,
                    "candidate_set_id": cset.id, "candidate_id": b["candidate_id"], "approval_id": decision.id,
                    "build_run_id": run.id, "positive": prompt.positive, "negative": prompt.negative},
        "config_snapshot_sha": item.snapshot_sha,
        "models": models,
        "engine": {k: v for k, v in cset.generation.items() if k not in ("models",)},
        "parameters": {"seed": b.get("seed"), **cset.generation.get("params", {})},
        "qa": None if qa is None else {"evaluation_id": qa.id, "status": qa.policy["status"],
                                       "coverage": qa.policy["coverage"], "override": decision.override_qa,
                                       "override_reason": decision.override_reason,
                                       "failed": decision.failed_checks, "missing": decision.missing_checks},
        "validation": run.validation,
        "licence": licence_summary(env.studio.settings.config_dir, used, cset.generation),
    }


def publish_pass(env: TaskEnv) -> dict[str, Any]:
    batch_id = env.op.payload["batch_id"]
    store = env.ctx.store
    out: dict[str, Any] = {}
    for entry in env.op.payload["items"]:
        env.check_cancel()
        item, _ = load_item(store, batch_id, entry["item_id"])
        run, _ = load_build(store, batch_id, entry["build_run_id"])
        mutate_item(env.studio, env.ctx, batch_id, item.id, lambda x: set_task(x, "publish", env.op.id, "running"))
        snap = store.read_snapshot(item.snapshot_sha)
        req = PublishRequest(
            op_id=f"{env.op.id}.{item.id}", idempotency_key=env.op.idempotency_key, artifacts=run.artifacts,
            preview_role="preview", origin=Origin.generated, asset_id=entry["asset_id"],
            new_asset=None if entry["asset_id"] else NewAsset(entry["name_id"], item.name, Kind(snap["recipe"]["kind"]),
                                                             Origin.generated, item.category_id),
            expected_current_version=entry.get("expected_current_version"), make_current=entry["make_current"],
            details=_details(env, batch_id, item, run))
        try:
            res = publish(store, req)
        except (StalePointer, Conflict, IntegrityError) as e:
            mutate_item(env.studio, env.ctx, batch_id, item.id,
                        lambda x, e=e: set_task(x, "publish", env.op.id, "failed", str(e)[:300]))
            out[item.id] = {"ok": False, "error": str(e)[:300]}
            continue
        env.ctx.index.upsert(store.get(manifest_key(res.asset_id), AssetManifest)[0])

        def apply(x: BatchItem, res: Any = res) -> None:
            x.published = Published(asset_id=res.asset_id, version_id=res.version_id,
                                    display_version=res.display_version)
            if x.target_asset_id is None:
                x.target_asset_id = res.asset_id
            set_task(x, "publish", env.op.id, "succeeded")
        mutate_item(env.studio, env.ctx, batch_id, item.id, apply)
        out[item.id] = {"ok": True, "asset_id": res.asset_id, "version_id": res.version_id,
                        "display_version": res.display_version}
    env.studio.events.publish("library", project_id=env.ctx.id)
    return {"items": out}


def publish_error(env: TaskEnv, state: str, message: str) -> None:
    _items_error(env, "publish", state, message, [e["item_id"] for e in env.op.payload["items"]])
