"""Node-mode readiness (R10, R13): everything is derived from what authorized, fresh runners advertise.

Studio never inspects its own /models or engines here. A runner counts only while it is active, its group exists,
its session is fresh and it has published an inventory.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from assetstudio_protocol.execution import OPERATION_CAPABILITY
from assetstudio_protocol.inventory import Inventory

from ..models import ModelStatus, load_lock
from ..studio import Studio
from .placement import catalog_sha256, session_fresh, slot_problem

__all__ = ["RunnerFacts", "fresh_inventories", "node_exporters", "node_model_statuses", "nodes_simulated",
           "node_gpus", "node_slot_urls", "operation_readiness", "runner_readiness"]

NO_RUNNER = "no runner is connected (active runner with a fresh session and inventory)"
_SEVERITY = {"corrupt": 3, "invalid_lock": 2, "missing": 1}  # receipt "unpinned" is reported as invalid_lock


@dataclass(frozen=True)
class RunnerFacts:
    runner_id: str
    name: str
    session: dict[str, Any]
    inventory: Inventory
    group: dict[str, Any]


def fresh_inventories(studio: Studio) -> list[RunnerFacts]:
    out: list[RunnerFacts] = []
    for runner in studio.auth.runners():
        if runner["state"] != "active" or (group := studio.auth.get_group(runner["group_id"])) is None:
            continue
        session = studio.journal.runners.active_session(runner["id"])
        if session is None or session["inventory"] is None or not session_fresh(studio, session):
            continue
        out.append(RunnerFacts(runner["id"], runner["name"], session, Inventory.model_validate(session["inventory"]),
                               group))
    return out


def _status(key: str, spec: dict[str, Any], status: str, detail: str) -> ModelStatus:
    files = spec.get("files") or {}
    return ModelStatus(key=key, repo=spec.get("repo", "?"), revision=str(spec.get("revision", "?")), status=status,  # type: ignore[arg-type]
                       licence=spec.get("licence", "unknown"), licence_status=spec.get("licence_status", "review"),
                       roles=list(spec.get("roles", [])), optional=bool(spec.get("optional")),
                       gated=bool(spec.get("gated")), detail=detail,
                       bytes_expected=sum(int(f.get("size") or 0) for f in files.values()), full_verified=True)


def _verdict(spec: dict[str, Any], key: str, seen: list[tuple[str, str, str]]) -> ModelStatus:
    """`seen` = (runner, receipt status, why) for every runner whose current-catalog receipt for the key is not ok."""
    if not seen:
        return _status(key, spec, "missing", "no connected runner verified this model")
    worst = max(seen, key=lambda s: _SEVERITY.get(s[1], 0))
    detail = "; ".join(f"runner {n}: {why}" for n, _, why in seen)
    return _status(key, spec, worst[1], detail)


def node_model_statuses(studio: Studio) -> dict[str, ModelStatus]:
    catalog_sha = catalog_sha256(studio)
    lock = load_lock(studio.settings.config_dir)["models"]
    facts = fresh_inventories(studio)
    out: dict[str, ModelStatus] = {}
    for key, spec in lock.items():
        seen: list[tuple[str, str, str]] = []
        runners_ok = []
        for f in facts:
            r = next((m for m in f.inventory.models if m.key == key), None)
            if r is None:
                continue
            if r.catalog_sha256 != catalog_sha:
                seen.append((f.name, "missing", "verified against another catalog"))
            elif r.status == "ok":
                runners_ok.append(f.name)
            else:
                seen.append((f.name, "invalid_lock" if r.status == "unpinned" else r.status, r.status))
        out[key] = (_status(key, spec, "ok", f"verified by runner {', '.join(runners_ok)}") if runners_ok
                    else _verdict(spec, key, seen))
    return out


def operation_readiness(studio: Studio, operation: str) -> tuple[bool, list[str]]:
    """True if some fresh slot could serve `operation` ignoring transient occupancy; else why none can."""
    facts = fresh_inventories(studio)
    if not facts:
        return False, [NO_RUNNER]
    reasons: list[str] = []
    for f in facts:
        ops = f.group["operations"]
        if ops != "*" and operation not in ops:
            reasons.append(f"runner {f.name}: operation not allowed for runner group")
            continue
        devices = {d["uuid"]: d for d in studio.journal.runners.devices(f.runner_id)}
        slots = studio.journal.runners.slots(f.runner_id)
        if not slots:
            reasons.append(f"runner {f.name}: no slots advertised")
        for slot in slots:
            if (p := slot_problem(slot, devices, set(), operation, transient=False)) is None:
                return True, []
            reasons.append(f"runner {f.name} slot {slot['slot_id']}: {p}")
    return False, reasons


def runner_readiness(studio: Studio) -> list[dict[str, Any]]:
    """Per operation readiness and reasons: the "why is nothing running" answer for the Runtime payload."""
    out = []
    for op in OPERATION_CAPABILITY:
        ready, reasons = operation_readiness(studio, op)
        out.append({"operation": op, "ready": ready, "reasons": reasons})
    return out


def nodes_simulated(studio: Studio) -> bool:
    facts = fresh_inventories(studio)
    return bool(facts) and all("simulated" in f.inventory.labels for f in facts)


def node_exporters(studio: Studio) -> dict[str, bool]:
    clean = research = False
    for f in fresh_inventories(studio):
        devices = {d["uuid"]: d for d in studio.journal.runners.devices(f.runner_id)}
        can_export = any(slot_problem(s, devices, set(), "worker3d.export", transient=False) is None
                         for s in studio.journal.runners.slots(f.runner_id))
        clean = clean or can_export
        research = research or (can_export and "exporter-research" in f.inventory.labels)
    return {"clean": clean, "research": research}


def node_gpus(studio: Studio) -> list[dict[str, Any]]:
    """Devices as runners advertise them. Usage is not reported, so it is 0 and `source` says so."""
    out = []
    for f in fresh_inventories(studio):
        claims = {d["uuid"]: d["claim"] for d in studio.journal.runners.devices(f.runner_id)}
        lane = {u: s.capability for s in f.inventory.slots for u in s.device_uuids}
        for d in f.inventory.devices:
            out.append({"index": str(d.index), "uuid": d.uuid, "name": d.name, "vram_used_mb": 0,
                        "vram_total_mb": d.memory_mb, "util_pct": 0, "measured_at": f.inventory.observed_at,
                        "source": f"runner {f.name} (usage not reported)", "lane": lane.get(d.uuid),
                        "ownership": {"claim": claims.get(d.uuid, "unknown")}})
    return out


def node_slot_urls(studio: Studio) -> dict[str, str]:
    """engine name -> "runner <name>/<slot>, ..." for the slots that run it."""
    found: dict[str, list[str]] = {}
    for f in fresh_inventories(studio):
        for slot in f.inventory.slots:
            for e in slot.engines:
                found.setdefault(e.engine, []).append(f"runner {f.name}/{slot.slot_id}")
    return {k: ", ".join(v) for k, v in found.items()}
