"""Runtime truth: physical GPUs, workers, model closure, licences, recipe readiness. Unknown is never shown as idle."""
from __future__ import annotations

import time
from typing import Any

from assetstudio_core.kinds import KINDS
from assetstudio_core.recipes import RECIPES

from ..gpu import nvidia_smi
from ..models import ModelStatus, verify_all
from ..provenance import load_licences
from ..studio import Studio

_CACHE: dict[str, tuple[float, Any]] = {}


def _cached(key: str, ttl: float, fn: Any) -> Any:
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    value = fn()
    _CACHE[key] = (now, value)
    return value


def model_statuses(studio: Studio) -> dict[str, ModelStatus]:
    return _cached(f"models:{id(studio)}", 30.0,
                   lambda: verify_all(studio.settings.config_dir, studio.settings.models_root))


def engine_check(studio: Studio) -> dict[str, Any]:
    if studio.engine is None:
        return {"reachable": False, "ready": False, "problems": ["no image engine configured (library-only mode)"]}
    return _cached(f"engine:{id(studio)}", 10.0, studio.engine.check)


def aux_health(studio: Studio) -> dict[str, Any]:
    if studio.aux is None:
        return {"reachable": False, "problems": ["no aux service configured"]}
    return _cached(f"aux:{id(studio)}", 5.0, studio.aux.health)


def recipe_readiness(studio: Studio) -> list[dict[str, Any]]:
    models = model_statuses(studio)
    eng = engine_check(studio)
    aux = aux_health(studio)
    out = []
    for r in RECIPES.values():
        gen_missing = [k for k in r.generation_models if not (models.get(k) and models[k].ready)]
        build_missing = [k for k in r.build_models if not (models.get(k) and models[k].ready)]
        if r.generation is None:
            gen = {"state": "unsupported_configuration", "reason": r.generation_blocked_reason}
        elif gen_missing:
            gen = {"state": "missing_models", "reason": f"missing: {', '.join(gen_missing)}", "missing": gen_missing}
        elif not eng.get("ready"):
            gen = {"state": "engine_unavailable", "reason": "; ".join(eng.get("problems", [])) or "engine not ready"}
        else:
            gen = {"state": "experimental" if studio.simulated else "ready",
                   "reason": "SIMULATED engine" if studio.simulated else ""}
        if r.build is None:
            state = "missing_models" if build_missing else "unsupported_configuration"
            build = {"state": state, "reason": r.build_blocked_reason, "missing": build_missing}
        elif build_missing:
            build = {"state": "missing_models", "reason": f"missing: {', '.join(build_missing)}",
                     "missing": build_missing}
        else:
            build = {"state": "ready", "reason": ""}
        qa_missing = [k for k in r.qa_models if not (models.get(k) and models[k].ready)]
        qa = {"state": "ready" if aux.get("reachable") and not qa_missing else "degraded",
              "reason": "" if aux.get("reachable") and not qa_missing else
              "advisory QA will report checks as unavailable (" + (", ".join(qa_missing) or "aux unreachable") + ")"}
        out.append({"id": r.id, "kind": r.kind.value, "label": KINDS[r.kind].label, "version": r.version,
                    "generation": gen, "build": build, "qa": qa,
                    "stages": [s.__dict__ for s in r.stages],
                    "params": [{**p.__dict__, "choices": list(p.choices)} for p in r.params],
                    "template": r.template, "negative": r.negative})
    return out


def worker_gpus(studio: Studio, eng: dict[str, Any], aux: dict[str, Any]) -> list[dict[str, Any]]:
    """GPU facts as reported by the workers that own them (the Studio container has no GPU/nvidia-smi)."""
    from assetstudio_core.canonical import now_iso

    out = []
    dev = (eng.get("devices") or [None])[0]
    if dev and dev.get("vram_total"):
        total, free = int(dev["vram_total"]), int(dev.get("vram_free") or 0)
        out.append({"index": studio.settings.gpu_ids["gpu0"], "uuid": "reported-by-comfyui",
                    "name": str(dev.get("name", "GPU")).split(" : ")[0].split(" ", 1)[-1],
                    "vram_used_mb": (total - free) // 2**20,
                    "vram_total_mb": total // 2**20, "util_pct": 0, "measured_at": now_iso(), "source": "comfyui"})
    g = aux.get("gpu")
    if isinstance(g, dict) and g.get("vram_total_mb"):
        out.append({"index": studio.settings.gpu_ids["gpu1"], "uuid": "reported-by-aux", "name": g.get("name", "GPU"),
                    "vram_used_mb": g["vram_used_mb"], "vram_total_mb": g["vram_total_mb"],
                    "util_pct": g.get("util_pct", 0), "measured_at": now_iso(), "source": "aux"})
    return out


def runtime(studio: Studio, coordinator: Any) -> dict[str, Any]:
    models = model_statuses(studio)
    licences = load_licences(studio.settings.config_dir)
    eng, aux = engine_check(studio), aux_health(studio)
    gpus = nvidia_smi() or ([] if studio.simulated else worker_gpus(studio, eng, aux))
    lane_by_index = {v: k for k, v in studio.settings.gpu_ids.items()}
    return {
        "simulated": studio.simulated,
        "engine_mode": studio.settings.engine,
        "gpus": [{**g, "lane": lane_by_index.get(g["index"]),
                  "ownership": studio.lanes[lane_by_index[g["index"]]].public()
                  if lane_by_index.get(g["index"]) in studio.lanes else None} for g in gpus],
        "services": [
            {"name": "comfyui", "role": "image generation · GPU0",
             "url": "SIMULATED (no ComfyUI contacted)" if studio.simulated else studio.settings.comfy_url,
             "reachable": eng.get("reachable", False), "ready": eng.get("ready", False),
             "problems": eng.get("problems", []), "version": eng.get("version"), "simulated": studio.simulated},
            {"name": "aux", "role": "text · VLM · segmentation · GPU1",
             "url": "SIMULATED (no aux service contacted)" if studio.simulated else studio.settings.aux_url,
             "reachable": aux.get("reachable", False), "ready": aux.get("reachable", False),
             "loaded": aux.get("loaded"), "problems": aux.get("problems", []), "simulated": studio.simulated},
        ],
        "lanes": {k: v.public() for k, v in studio.lanes.items()},
        "coordinator": coordinator.status() if coordinator is not None else None,
        "models": [{**m.__dict__, "ready": m.ready, "licence_record": licences.get(k)} for k, m in models.items()],
        "licences": [{"id": k, **v} for k, v in licences.items()],
        "recipes": recipe_readiness(studio),
    }
