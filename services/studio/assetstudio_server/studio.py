"""Application container: registry, journal, engines, GPU lanes, events. One per Studio process."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .adapters.aux import AuxClient
from .adapters.base import AuxService, ImageEngine, Worker3dService
from .adapters.comfyui import ComfyEngine, WorkflowRegistry
from .adapters.fake import FakeAux, FakeEngine, FakeWorker3d
from .adapters.worker3d import Worker3dClient
from .authstore import AuthStore
from .events import EventBus
from .execution import DirectBackend, ExecutionBackend
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
    auth: AuthStore
    events: EventBus
    engine: ImageEngine | None
    aux: AuxService | None
    worker3d: Worker3dService | None
    lanes: dict[str, GpuLane]
    hash_cache: HashCache
    extras: dict[str, Any] = field(default_factory=dict)
    execution: ExecutionBackend = field(init=False)  # set by build_studio once the Studio exists

    @property
    def simulated(self) -> bool:
        return any(getattr(x, "simulated", False) for x in (self.engine, self.aux, self.worker3d))

    def close(self) -> None:
        self.registry.close_all()
        self.journal.close()
        self.auth.close()


def build_studio(settings: Settings, engine: ImageEngine | None = None, aux: AuxService | None = None,
                 worker3d: Worker3dService | None = None) -> Studio:
    if settings.execution == "nodes":
        raise ValueError("node execution arrives with the remote adapters (WP2.4); set STUDIO_EXECUTION=direct")
    settings.ensure()
    if engine is None and aux is None:
        if settings.engine == "comfyui":
            engine = ComfyEngine(settings.comfy_url, WorkflowRegistry(settings.workflows_dir))
            aux = AuxClient(settings.aux_url)
            # An empty WORKER3D_URL disables 3D; otherwise its GPU1 release must be acknowledged like aux.
            worker3d = Worker3dClient(settings.worker3d_url) if settings.worker3d_url else None
        elif settings.engine == "fake":
            engine, aux, worker3d = FakeEngine(), FakeAux(), FakeWorker3d()
    journal = Journal(settings.instance_dir / "journal" / "operations.sqlite")
    workers = {w.name: LaneWorker(w.lease, w.unload) for w in (aux, worker3d) if w is not None}
    lanes = {"gpu1": GpuLane("gpu1", workers, lambda: journal.next_epoch("gpu1"))}
    studio = Studio(settings=settings, registry=Registry(settings), journal=journal,
                  auth=AuthStore(settings.instance_dir / "auth.sqlite"), events=EventBus(),
                  engine=engine, aux=aux, worker3d=worker3d, lanes=lanes,
                  hash_cache=HashCache(settings.instance_dir / "model-hashes.json"))
    studio.execution = DirectBackend(studio)
    return studio
