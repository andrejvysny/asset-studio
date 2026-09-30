"""Executes offers against the real engine adapters (or the fake engines when simulated).

Call semantics mirror Studio's stages: image prompts are reconciled by their deterministic prompt id before any
(re)submit, aux and worker3d calls run under a runner-local GpuLane epoch, and an uncertain engine outcome is a
resource problem (ExecutionBlocked), never a verdict on the item.
"""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from assetstudio_protocol import calls
from assetstudio_protocol import engine as eng
from assetstudio_protocol.execution import InputRef, Offer

from .admission import GpuLane, LaneWorker, OwnershipUnknown
from .config import ConfigError, RunnerConfig
from .engines.aux import AuxClient
from .engines.comfyui import ComfyEngine, WorkflowRegistry
from .engines.fake import FakeAux, FakeEngine, FakeWorker3d
from .engines.worker3d import Worker3dClient
from .executor import ExecutionBlocked, ExecutionCancelled, ExecutionFailed
from .image_calls import Poll, run_edit, run_t2i
from .state import RunnerState

Output = tuple[str, bytes, str]
Blobs = list[tuple[InputRef, bytes]]
_PASS_CODES = ("input_invalid", "oom")
_TIMEOUT_S = 30 * 60


@dataclass
class Engines:
    comfy: Any = None
    aux: Any = None
    worker3d: Any = None


def build_engines(config: RunnerConfig) -> Engines:
    """Fake engines when simulated, else clients for the configured URLs. ConfigError names what is missing."""
    names = {e for sc in config.slots for e in sc.engines}
    if config.simulated:
        return Engines(FakeEngine() if "comfyui" in names else None, FakeAux() if "aux" in names else None,
                       FakeWorker3d() if "worker3d" in names else None)
    urls = config.engines
    for name in sorted(names):
        if getattr(urls, name) is None:
            raise ConfigError(f"engines.{name} url is required by a configured slot")
    comfy = None
    if urls.comfyui and "comfyui" in names:
        if config.workflows_dir is None:
            raise ConfigError("workflows_dir is required for an image slot")
        comfy = ComfyEngine(urls.comfyui, WorkflowRegistry(config.workflows_dir))
    return Engines(comfy, AuxClient(urls.aux) if urls.aux and "aux" in names else None,
                   Worker3dClient(urls.worker3d) if urls.worker3d and "worker3d" in names else None)


@contextmanager
def _mapped() -> Iterator[None]:
    """Engine-side exceptions -> the agent's executor vocabulary."""
    try:
        yield
    except eng.ExecutionFailed as e:  # before EngineRejected: it is a subclass
        if e.code == "cancelled":
            raise ExecutionCancelled() from e
        raise ExecutionFailed(e.code if e.code in _PASS_CODES else "internal", str(e)) from e  # type: ignore[arg-type]
    except eng.ExecutionCancelled as e:
        raise ExecutionCancelled() from e
    except eng.ExecutionLost as e:
        raise ExecutionFailed("internal", f"worker lost the execution: {e}", lost=True) from e
    except eng.EngineRejected as e:
        raise ExecutionFailed("input_invalid", str(e)) from e
    except eng.EngineUnavailable as e:
        raise ExecutionBlocked("node_unavailable", str(e)) from e
    except OwnershipUnknown as e:
        raise ExecutionBlocked("admission_rejected", str(e)) from e


def _single(blobs: Blobs, role: str) -> bytes:
    if len(blobs) != 1 or blobs[0][0].role != role:
        raise ExecutionFailed("input_invalid", f"expected exactly one input with role {role!r}")
    return blobs[0][1]


class EngineExecutor:
    # Engine calls are keyed by Studio-chosen ids (prompt / execution id): re-running an interrupted call finds the
    # engine's work instead of starting it again.
    reconciles = True

    def __init__(self, config: RunnerConfig, state: RunnerState, engines: Engines, *,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
                 poll_s: float | None = None, timeout_s: float = _TIMEOUT_S) -> None:
        self.config = config
        self.engines = engines
        self._poll = Poll(sleep, clock, config.poll_s if poll_s is None else poll_s, timeout_s)
        self.lanes: dict[str, GpuLane] = {sc.slot_id: self._make_lane(sc.slot_id, sc.engines, state)
                                          for sc in config.slots if sc.capability == "aux3d"}

    def _make_lane(self, slot_id: str, names: list[str], state: RunnerState) -> GpuLane:
        workers = {n: LaneWorker(lease=c.lease, unload=c.unload)
                   for n in names if (c := getattr(self.engines, n, None)) is not None}
        return GpuLane(name=slot_id, workers=workers, next_epoch=lambda: state.next_epoch(slot_id))

    # -- Executor protocol ------------------------------------------------------------------------------------------

    def execute(self, offer: Offer, inputs: dict[str, Path], out_dir: Path,
                should_cancel: Callable[[], bool]) -> tuple[list[tuple[str, Path, str]], dict[str, Any]]:
        try:
            params = calls.parse_params(offer.operation, offer.params)
            blobs = [(ref, inputs[ref.sha256].read_bytes()) for ref in offer.inputs]
        except (ValueError, KeyError, OSError) as e:
            raise ExecutionFailed("input_invalid", f"{offer.operation}: {str(e)[:300]}") from e
        with _mapped():
            outputs, meta = self._dispatch(offer, params, blobs, should_cancel)
        out_dir.mkdir(parents=True, exist_ok=True)
        files = []
        for name, data, mime in outputs:
            (out_dir / name).write_bytes(data)
            files.append((name, out_dir / name, mime))
        return files, meta

    def spooled(self, offer: Offer) -> None:
        """R8/I07: the 3D worker may drop its copy only once the result manifest is durable here."""
        if not offer.operation.startswith("worker3d.") or self.engines.worker3d is None:
            return
        params = calls.parse_params(offer.operation, offer.params)
        self.engines.worker3d.ack(params.execution_id)  # type: ignore[attr-defined]

    # -- dispatch ---------------------------------------------------------------------------------------------------

    def _dispatch(self, offer: Offer, p: Any, blobs: Blobs,
                  should_cancel: Callable[[], bool]) -> tuple[list[Output], dict[str, Any]]:
        op = offer.operation
        if op.startswith("image."):
            return self._image(op, p, blobs, should_cancel)
        if op.startswith("aux."):
            return self._aux(offer, p, blobs)
        return self._worker3d(offer, p, _single(blobs, "body"), should_cancel)

    def _engine(self, name: str) -> Any:
        found = getattr(self.engines, name)
        if found is None:
            raise ExecutionBlocked("node_unavailable", f"no {name} engine configured on this runner")
        return found

    def _acquire(self, slot_id: str, worker: str) -> int:
        lane = self.lanes.get(slot_id)
        if lane is None or worker not in lane.workers:
            raise ExecutionBlocked("node_unavailable", f"slot {slot_id} has no {worker} engine")
        return lane.acquire(worker)

    def _image(self, op: str, p: Any, blobs: Blobs, should_cancel: Callable[[], bool]) -> tuple[list[Output], dict]:
        engine = self._engine("comfy")
        if op == "image.t2i":
            data, meta = run_t2i(engine, p, should_cancel, self._poll)
        else:
            data, meta = run_edit(engine, p, _single(blobs, "image"), should_cancel, self._poll)
        return [("image.png", data, "image/png")], meta

    def _aux(self, offer: Offer, p: Any, blobs: Blobs) -> tuple[list[Output], dict[str, Any]]:
        aux = self._engine("aux")
        if offer.operation in ("aux.qa", "aux.cutout"):
            _single(blobs, "image")  # refuse before a GPU handoff is spent on an invalid call
        epoch = self._acquire(offer.slot_id, "aux")
        res = _call_aux(aux, offer.operation, p, blobs, epoch)
        doc, files = calls.encode_result(res)
        outputs: list[Output] = [("result.json", doc, "application/json"),
                                 *((k, v, "application/octet-stream") for k, v in files.items())]
        return outputs, {**(res.get("meta") or {}), "simulated": aux.simulated}

    def _worker3d(self, offer: Offer, p: calls.Worker3dParams, body: bytes,
                  should_cancel: Callable[[], bool]) -> tuple[list[Output], dict[str, Any]]:
        if offer.operation != f"worker3d.{p.op}":
            raise ExecutionFailed("input_invalid", f"operation {offer.operation} does not match op {p.op!r}")
        w = self._engine("worker3d")
        epoch = self._acquire(offer.slot_id, "worker3d")
        data, meta = w.execute(p.execution_id, p.op, p.params, body, epoch=epoch, should_cancel=should_cancel)
        return [("result.bin", data, "application/octet-stream")], {**meta, "simulated": w.simulated}


def _call_aux(aux: Any, op: str, p: Any, blobs: Blobs, epoch: int) -> dict[str, Any]:
    common = {"epoch": epoch, "execution_id": p.execution_id}
    triples = [(data, ref.role, ref.label) for ref, data in blobs]
    pairs = [(data, ref.role) for ref, data in blobs]
    if op == "aux.enhance":
        return aux.enhance(brief=p.brief, kind=p.kind, constraints=p.constraints, style_guide=p.style_guide,
                           preset=p.preset, mode=p.mode, images=triples or None, preserve=p.preserve,
                           change=p.change, **common)
    if op == "aux.compare":
        return aux.compare(images=triples, questions=list(p.questions), context=p.context, **common)
    if op == "aux.qa":
        return aux.qa(image=_single(blobs, "image"), questions=list(p.questions), context=p.context, **common)
    if op == "aux.cutout":
        return aux.cutout(image=_single(blobs, "image"), **common)
    if op == "aux.analyze_source":
        return aux.analyze_source(images=pairs, kind=p.kind, user_facts=p.user_facts, **common)
    return aux.suggest_variants(images=pairs, request=p.request, count=p.count, intent=p.intent,
                                preserve=p.preserve, kind=p.kind, observations=p.observations, **common)
