"""Inventory built from the slot configuration, live GPU facts (nvidia-smi), slot states and model receipts."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from assetstudio_protocol.execution import OPERATION_ENGINE, OPERATION_VERSIONS
from assetstudio_protocol.inventory import Device, EngineInfo, Inventory, ModelReceipt, OperationRef, Slot

from .admission import nvidia_smi
from .config import ConfigError, RunnerConfig, SlotConfig


def _engine_info(engine: str) -> EngineInfo:
    ops = [OperationRef(op=op, version=OPERATION_VERSIONS[op]) for op, e in OPERATION_ENGINE.items() if e == engine]
    return EngineInfo(engine=engine, version="configured", operations=ops)


def _static_device(ref: str, position: int, runner_id: str) -> Device:
    if ref.startswith("index:"):
        n = int(ref.removeprefix("index:"))
        return Device(uuid=f"{runner_id}/{n}", index=n, name="configured", memory_mb=0, fallback=True)
    return Device(uuid=ref, index=position, name="configured", memory_mb=0)


def _real_device(gpu: dict[str, Any]) -> Device:
    return Device(uuid=gpu["uuid"], index=int(gpu["index"]), name=gpu["name"], memory_mb=gpu["vram_total_mb"])


def _device(ref: str, position: int, runner_id: str, gpus: list[dict[str, Any]]) -> Device:
    if not gpus:
        return _static_device(ref, position, runner_id)
    if ref.startswith("index:"):
        found = next((g for g in gpus if g["index"] == ref.removeprefix("index:")), None)
    else:
        found = next((g for g in gpus if g["uuid"] == ref), None)
    if found is None:
        raise ConfigError(f"configured device {ref!r} not found by nvidia-smi "
                          f"(present: {', '.join(g['uuid'] for g in gpus)})")
    return _real_device(found)


def resolve_devices(config: RunnerConfig, runner_id: str, gpus: list[dict[str, Any]]) -> dict[str, list[Device]]:
    """slot_id -> devices. With nvidia-smi output every configured device must exist (ConfigError otherwise)."""
    out: dict[str, list[Device]] = {}
    seen: dict[str, str] = {}
    position = 0
    for sc in config.slots:
        devs = []
        for ref in sc.devices:
            dev = _device(ref, position, runner_id, gpus)
            if dev.uuid in seen:
                raise ConfigError(f"device {ref!r} resolves to {dev.uuid}, already claimed by slot {seen[dev.uuid]}")
            seen[dev.uuid] = sc.slot_id
            devs.append(dev)
            position += 1
        out[sc.slot_id] = devs
    return out


def _slot(cfg: SlotConfig, uuids: list[str], state: str) -> Slot:
    return Slot(slot_id=cfg.slot_id, capability=cfg.capability, device_uuids=uuids,
                engines=[_engine_info(e) for e in cfg.engines], state=state)  # type: ignore[arg-type]


def build_inventory(config: RunnerConfig, revision: int, catalog_sha: str, *, runner_id: str,
                    models: list[ModelReceipt] | None = None, slot_states: dict[str, str] | None = None,
                    gpus: list[dict[str, Any]] | None = None) -> Inventory:
    """`gpus=None` queries nvidia-smi unless the runner is simulated; an empty list keeps the configured values."""
    if gpus is None:
        gpus = [] if config.simulated else nvidia_smi()
    by_slot = resolve_devices(config, runner_id, gpus)
    states = slot_states or {}
    devices = [d for sc in config.slots for d in by_slot[sc.slot_id]]
    slots = [_slot(sc, [d.uuid for d in by_slot[sc.slot_id]], states.get(sc.slot_id, "ready")) for sc in config.slots]
    return Inventory(schema="assetstudio.runner.inventory.v1", revision=revision,
                     observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), devices=devices, slots=slots,
                     models=models or [], labels=config.labels)
