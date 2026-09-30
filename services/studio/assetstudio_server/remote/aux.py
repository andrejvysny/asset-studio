"""Remote AuxService: every aux operation is one runner call; results come back as result.json plus binary files."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from assetstudio_protocol import calls as pc

from .calls import image_mime, result_simulated, run_stage_call

if TYPE_CHECKING:
    from ..coordinator.runner import TaskEnv
    from ..studio import Studio

__all__ = ["RemoteAux"]


def _triples(images: list[tuple[bytes, str, str]] | None) -> list[tuple[bytes, str, str, str]]:
    return [(data, role, label, image_mime(data)) for data, role, label in images or []]


def _pairs(images: list[tuple[bytes, str]]) -> list[tuple[bytes, str, str, str]]:
    return [(data, view, "", image_mime(data)) for data, view in images]


class RemoteAux:
    name = "aux"

    def __init__(self, studio: Studio, env: TaskEnv | None) -> None:
        self.studio, self.env = studio, env

    @property
    def simulated(self) -> bool:
        return self.env is not None and result_simulated(self.studio, self.env.task.id)

    def _call(self, operation: str, params: Any, inputs: list[tuple[bytes, str, str, str]]) -> dict[str, Any]:
        assert self.env is not None, "an unbound RemoteAux cannot run calls"
        files, _ = run_stage_call(self.studio, self.env, operation, params, inputs)
        return pc.decode_result(files["result.json"], files)

    # execution_id is not sent: it would change the offer digest on every task retry (stale_revision); the attempt
    # id is the call's identity instead.
    def enhance(self, *, brief: str, kind: str, constraints: str, style_guide: str, epoch: int,
                execution_id: str | None = None, preset: str = "conservative", mode: str = "t2i",
                images: list[tuple[bytes, str, str]] | None = None, preserve: str = "",
                change: str = "") -> dict[str, Any]:
        params = pc.AuxEnhanceParams(brief=brief, kind=kind, constraints=constraints, style_guide=style_guide,
                                     preset=preset, mode=mode, preserve=preserve, change=change)
        return self._call("aux.enhance", params, _triples(images))

    def compare(self, *, images: list[tuple[bytes, str, str]], questions: list[tuple[str, str]], context: str,
                epoch: int, execution_id: str | None = None) -> dict[str, Any]:
        params = pc.AuxCompareParams(questions=questions, context=context)
        return self._call("aux.compare", params, _triples(images))

    def analyze_source(self, *, images: list[tuple[bytes, str]], kind: str, user_facts: str = "", epoch: int,
                       execution_id: str | None = None) -> dict[str, Any]:
        return self._call("aux.analyze_source", pc.AuxAnalyzeParams(kind=kind, user_facts=user_facts),
                          _pairs(images))

    def suggest_variants(self, *, images: list[tuple[bytes, str]], request: str, count: int, intent: str,
                         preserve: str, kind: str, observations: list[str] | None = None, epoch: int,
                         execution_id: str | None = None) -> dict[str, Any]:
        params = pc.AuxSuggestParams(request=request, count=count, intent=intent, preserve=preserve, kind=kind,
                                     observations=observations)
        return self._call("aux.suggest_variants", params, _pairs(images))

    def qa(self, *, image: bytes, questions: list[tuple[str, str]], context: str, epoch: int,
           execution_id: str | None = None) -> dict[str, Any]:
        return self._call("aux.qa", pc.AuxQaParams(questions=questions, context=context),
                          [(image, "image", "", image_mime(image))])

    def cutout(self, *, image: bytes, epoch: int, execution_id: str | None = None) -> dict[str, Any]:
        return self._call("aux.cutout", pc.AuxCutoutParams(), [(image, "image", "", image_mime(image))])

    # Worker-lease surface: node mode never acquires a Studio-side GPU lease (runner-local admission).
    def health(self) -> dict[str, Any]:
        return {"reachable": True, "loads": None}

    def lease(self, epoch: int) -> dict[str, Any]:
        return {"leased": False, "reason": "runner-local admission"}

    def unload(self, owner_token: str, epoch: int) -> dict[str, Any]:
        return {"loaded": False, "reason": "runner-local admission"}
