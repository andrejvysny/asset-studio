"""Runner agent configuration (docs/modular/compute-runner.md R7, R17)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from assetstudio_core.safeyaml import load_yaml
from assetstudio_protocol.base import Label
from assetstudio_protocol.execution import Capability, Engine
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_AUX3D_ENGINES = frozenset({"aux", "worker3d"})
_INDEX_RE = re.compile(r"^index:\d+$")
_LISTEN_RE = re.compile(r"^[^:\s]+:\d{1,5}$")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EngineUrls(_Strict):
    comfyui: str | None = None
    aux: str | None = None
    worker3d: str | None = None


class SlotConfig(_Strict):
    slot_id: Label
    capability: Capability
    devices: list[str] = Field(min_length=1)  # physical UUIDs, or "index:N" when the UUID is unavailable
    engines: list[Engine] = Field(min_length=1)

    @model_validator(mode="after")
    def _engines_match_capability(self) -> SlotConfig:
        names = set(self.engines)
        if self.capability == "image" and names != {"comfyui"}:
            raise ValueError(f"image slot {self.slot_id} may only run comfyui, got {sorted(names)}")
        if self.capability == "aux3d" and not names <= _AUX3D_ENGINES:
            raise ValueError(f"aux3d slot {self.slot_id} may only run aux/worker3d, got {sorted(names)}")
        if len(set(self.devices)) != len(self.devices) or not all(d.strip() for d in self.devices):
            raise ValueError(f"slot {self.slot_id}: devices must be unique and non-empty")
        return self


class RunnerConfig(_Strict):
    studio_url: str
    name: str = Field(min_length=1, max_length=64)
    state_dir: Path
    host_lock: Path = Path("/var/lib/assetstudio-runner/host.lock")
    key_file: Path | None = None
    registration_token_file: Path | None = None
    dispatch: Literal["pull", "push"] = "pull"
    push_listen: str | None = None
    engines: EngineUrls = EngineUrls()
    slots: list[SlotConfig] = Field(min_length=1)
    labels: list[Label] = []
    models_root: Path | None = None
    workflows_dir: Path | None = None  # ComfyUI workflow bindings; required for a real (non-simulated) image slot
    poll_s: float = Field(default=0.5, gt=0)  # image engine status poll interval
    acquire_wait_s: int = Field(default=25, ge=0, le=50)
    simulated: bool = False

    @model_validator(mode="after")
    def _check(self) -> RunnerConfig:
        ids = [s.slot_id for s in self.slots]
        if len(set(ids)) != len(ids):
            raise ValueError("slot_ids must be unique")
        claimed: dict[str, str] = {}
        for s in self.slots:
            for d in s.devices:
                if d.startswith("index:") and not _INDEX_RE.match(d):
                    raise ValueError(f"slot {s.slot_id}: malformed device {d!r} (expected index:N)")
                if d in claimed:
                    raise ValueError(f"device {d!r} appears in slots {claimed[d]} and {s.slot_id} (overlap)")
                claimed[d] = s.slot_id
        if self.dispatch == "push" and not self.push_listen:
            raise ValueError("push_listen is required when dispatch is push")
        if self.push_listen and not _LISTEN_RE.match(self.push_listen):
            raise ValueError("push_listen must be host:port")
        return self

    @property
    def key_path(self) -> Path:
        return self.key_file or self.state_dir / "runner.key"


class ConfigError(ValueError):
    pass


def load_config(path: Path) -> RunnerConfig:
    try:
        data = load_yaml(path.read_bytes())
        return RunnerConfig.model_validate(data)
    except (OSError, ValueError, ValidationError) as e:  # ParseError and ValidationError are ValueErrors
        raise ConfigError(f"{path}: {e}") from e
