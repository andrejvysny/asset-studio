"""Application container: registry, journal, engines, GPU lanes, events. One per Studio process."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .adapters.aux import AuxClient
from .adapters.base import AuxService, ImageEngine, Worker3dService
from .adapters.comfyui import ComfyEngine, Workflow
from .adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from .adapters.worker3d import Worker3dClient
from .events import EventBus
from .gpu import GpuLane, LaneWorker
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
    worker3d: Worker3dService | None
    lanes: dict[str, GpuLane]
    hash_cache: HashCache
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def simulated(self) -> bool:
        return any(getattr(x, "simulated", False) for x in (self.engine, self.aux, self.worker3d))

    def close(self) -> None:
        self.registry.close_all()
        self.journal.close()


def build_studio(settings: Settings, engine: ImageEngine | None = None, aux: AuxService | None = None,
                 worker3d: Worker3dService | None = None) -> Studio:
    settings.ensure()
    if engine is None and aux is None:
        if settings.engine == "comfyui":
            engine = ComfyEngine(settings.comfy_url, Workflow(settings.workflows_dir, "image.t2i.qwen.bindings.yaml"))
            aux = AuxClient(settings.aux_url)
            # An empty WORKER3D_URL disables 3D; otherwise its GPU1 release must be acknowledged like aux.
            worker3d = Worker3dClient(settings.worker3d_url) if settings.worker3d_url else None
        elif settings.engine == "fake":
            engine, aux, worker3d = FakeEngine(), FakeAux(), FakeWorker3d()
    journal = Journal(settings.instance_dir / "journal" / "operations.sqlite")
    workers = {w.name: LaneWorker(w.lease, w.unload) for w in (aux, worker3d) if w is not None}
    lanes = {"gpu1": GpuLane("gpu1", workers, lambda: journal.next_epoch("gpu1"))}
    return Studio(settings=settings, registry=Registry(settings), journal=journal, events=EventBus(),
                  engine=engine, aux=aux, worker3d=worker3d, lanes=lanes,
                  hash_cache=HashCache(settings.instance_dir / "model-hashes.json"))
