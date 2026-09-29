"""Application container: registry, journal, engines, GPU lanes, events. One per Studio process."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .adapters.aux import AuxClient
from .adapters.base import AuxService, ImageEngine
from .adapters.comfyui import ComfyEngine, Workflow
from .adapters.fake import FakeAux, FakeEngine
from .events import EventBus
from .gpu import GpuLane
from .journal import Journal
from .models import HashCache
from .registry import Registry
from .settings import Settings

log = logging.getLogger("assetstudio")


@dataclass
class Studio:
    settings: Settings
    registry: Registry
    journal: Journal
    events: EventBus
    engine: ImageEngine | None
    aux: AuxService | None
    lanes: dict[str, GpuLane]
    hash_cache: HashCache
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def simulated(self) -> bool:
        return bool(getattr(self.engine, "simulated", False) or getattr(self.aux, "simulated", False))

    def close(self) -> None:
        self.registry.close_all()
        self.journal.close()


def build_studio(settings: Settings, engine: ImageEngine | None = None, aux: AuxService | None = None) -> Studio:
    settings.ensure()
    if engine is None and aux is None:
        if settings.engine == "comfyui":
            engine = ComfyEngine(settings.comfy_url, Workflow(settings.workflows_dir, "image.t2i.qwen.bindings.yaml"))
            aux = AuxClient(settings.aux_url)
        elif settings.engine == "fake":
            engine, aux = FakeEngine(), FakeAux()
    unloaders = {"aux": aux.unload} if aux is not None else {}
    lanes = {"gpu1": GpuLane("gpu1", unloaders)}
    return Studio(settings=settings, registry=Registry(settings),
                  journal=Journal(settings.instance_dir / "journal" / "operations.sqlite"), events=EventBus(),
                  engine=engine, aux=aux, lanes=lanes,
                  hash_cache=HashCache(settings.instance_dir / "model-hashes.json"))
