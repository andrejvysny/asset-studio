"""Runner inventory: devices, slots, model receipts (R7, R10)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, field_validator, model_validator

from .base import Label, Msg, Sha256, Timestamp
from .execution import Capability, Engine, Operation

__all__ = ["Capability", "Engine", "SlotState", "Device", "OperationRef", "EngineInfo", "Slot", "ModelReceipt",
           "Inventory"]

SlotState = Literal["installed", "loaded", "ready", "busy", "unreachable", "unknown"]
_AUX3D_ENGINES = frozenset({"aux", "worker3d"})


class Device(Msg):
    uuid: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    index: int = Field(ge=0)
    name: str
    memory_mb: int = Field(ge=0)
    fallback: bool = False  # True when uuid is a `runner/index` fallback


class OperationRef(Msg):
    op: Operation
    version: int = Field(ge=1)


class EngineInfo(Msg):
    engine: Engine
    version: str
    operations: list[OperationRef]


class Slot(Msg):
    slot_id: Label
    capability: Capability
    device_uuids: list[str] = Field(min_length=1)
    engines: list[EngineInfo] = Field(min_length=1)
    loaded_residency: str | None = None
    state: SlotState

    @model_validator(mode="after")
    def _engines_match_capability(self) -> Slot:
        names = {e.engine for e in self.engines}
        if self.capability == "image" and names != {"comfyui"}:
            raise ValueError(f"image slot {self.slot_id} may only run comfyui, got {sorted(names)}")
        if self.capability == "aux3d" and not names <= _AUX3D_ENGINES:
            raise ValueError(f"aux3d slot {self.slot_id} may only run aux/worker3d, got {sorted(names)}")
        return self


class ModelReceipt(Msg):
    key: str
    revision: str
    files_sha256: Sha256
    verification: Literal["full"]
    status: Literal["ok", "missing", "corrupt", "unpinned"]
    catalog_sha256: Sha256
    verified_at: Timestamp


class Inventory(Msg):
    schema_: Literal["assetstudio.runner.inventory.v1"] = Field(alias="schema")
    revision: int = Field(ge=0)
    observed_at: Timestamp
    devices: list[Device]
    slots: list[Slot]
    models: list[ModelReceipt] = []
    labels: list[Label] = []

    @field_validator("devices")
    @classmethod
    def _unique_devices(cls, v: list[Device]) -> list[Device]:
        uuids = [d.uuid for d in v]
        if len(set(uuids)) != len(uuids):
            raise ValueError("device uuids must be unique")
        return v

    @model_validator(mode="after")
    def _check_slots(self) -> Inventory:
        ids = [s.slot_id for s in self.slots]
        if len(set(ids)) != len(ids):
            raise ValueError("slot_ids must be unique")
        known = {d.uuid for d in self.devices}
        claimed: dict[str, str] = {}
        for s in self.slots:
            for u in s.device_uuids:
                if u not in known:
                    raise ValueError(f"slot {s.slot_id} references unknown device {u!r}")
                if u in claimed:
                    raise ValueError(f"device {u!r} appears in slots {claimed[u]} and {s.slot_id} (overlap)")
                claimed[u] = s.slot_id
        return self
