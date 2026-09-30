"""Static inventory built from the slot configuration (live discovery and model verification arrive in P2)."""
from __future__ import annotations

from datetime import UTC, datetime

from assetstudio_protocol.execution import OPERATION_ENGINE, OPERATION_VERSIONS
from assetstudio_protocol.inventory import Device, EngineInfo, Inventory, ModelReceipt, OperationRef, Slot

from .config import RunnerConfig, SlotConfig


def _engine_info(engine: str) -> EngineInfo:
    ops = [OperationRef(op=op, version=OPERATION_VERSIONS[op]) for op, e in OPERATION_ENGINE.items() if e == engine]
    return EngineInfo(engine=engine, version="configured", operations=ops)


def _device(ref: str, position: int, runner_id: str) -> Device:
    if ref.startswith("index:"):
        n = int(ref.removeprefix("index:"))
        return Device(uuid=f"{runner_id}/{n}", index=n, name="configured", memory_mb=0, fallback=True)
    return Device(uuid=ref, index=position, name="configured", memory_mb=0)


def _slot(cfg: SlotConfig, uuids: list[str]) -> Slot:
    return Slot(slot_id=cfg.slot_id, capability=cfg.capability, device_uuids=uuids,
                engines=[_engine_info(e) for e in cfg.engines], state="ready")


def build_inventory(config: RunnerConfig, revision: int, catalog_sha: str, *, runner_id: str,
                    models: list[ModelReceipt] | None = None) -> Inventory:
    # P2: model receipts. catalog_sha is the hook for verifying models against the session catalog.
    devices: list[Device] = []
    slots: list[Slot] = []
    for sc in config.slots:
        uuids = []
        for ref in sc.devices:
            dev = _device(ref, len(devices), runner_id)
            devices.append(dev)
            uuids.append(dev.uuid)
        slots.append(_slot(sc, uuids))
    return Inventory(schema="assetstudio.runner.inventory.v1", revision=revision,
                     observed_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), devices=devices, slots=slots,
                     models=models or [], labels=config.labels)
